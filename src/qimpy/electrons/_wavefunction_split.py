from __future__ import annotations
import qimpy as qp
import numpy as np
import torch
from dataclasses import dataclass
from mpi4py import MPI


def _split_bands(
    self: qp.electrons.Wavefunction,
) -> qp.utils.Waitable[qp.electrons.Wavefunction]:
    """Return wavefunction split by bands, bringing all basis coefficients of
    each band together on some process. Note that the result may be a view if
    there is only one process, or if the wavefunction is already split by bands
    """
    if (self.basis.division.n_procs == 1) or self.band_division:
        return qp.utils.Waitless(self)  # already in required configuration
    basis = self.basis

    # Bring band-dimension to outermost (so that send chunks are contiguous):
    # --- after this, dim order is (band, spin, k, spinor, basis)
    send_coeff = self.coeff.permute(2, 0, 1, 3, 4).contiguous()
    n_per_band = np.prod(send_coeff.shape[1:])

    # All-to-all MPI rearrangement:
    band_division = qp.utils.TaskDivision(
        n_tot=self.coeff.shape[2],
        n_procs=basis.division.n_procs,
        i_proc=basis.division.i_proc,
    )
    send_counts = np.diff(band_division.n_prev) * n_per_band
    send_offset = band_division.n_prev[:-1] * n_per_band
    recv_counts = band_division.n_mine * n_per_band
    recv_offset = np.arange(band_division.n_procs) * recv_counts
    mpi_type = qp.rc.mpi_type[send_coeff.dtype]
    recv_coeff = torch.zeros(
        (band_division.n_procs, band_division.n_mine) + send_coeff.shape[1:],
        dtype=send_coeff.dtype,
        device=send_coeff.device,
    )
    qp.rc.current_stream_synchronize()
    request = basis.comm.Ialltoallv(
        (qp.utils.BufferView(send_coeff), send_counts, send_offset, mpi_type),
        (qp.utils.BufferView(recv_coeff), recv_counts, recv_offset, mpi_type),
    )
    return SplitBandsWait(request, send_coeff, recv_coeff, basis, band_division)


@dataclass
class SplitBandsWait:
    """`Waitable` object for the result of `Wavefunction.split_bands`."""

    request: MPI.Request
    send_coeff: torch.Tensor
    recv_coeff: torch.Tensor
    basis: qp.electrons.Basis
    band_division: qp.utils.TaskDivision

    def wait(self) -> qp.electrons.Wavefunction:
        """Complete `Wavefunction.split_bands` after waiting on MPI transfers."""
        # Wait for MPI completion:
        self.request.Wait()
        del self.send_coeff
        # Unscramble data to bring all basis for each band together:
        # --- before this data order is (proc, band, spin, k, spinor, basis)
        result = qp.electrons.Wavefunction(
            self.basis,
            coeff=self.recv_coeff.permute(2, 3, 1, 4, 0, 5).flatten(4, 5),
            band_division=self.band_division,
        )
        del self.recv_coeff
        return result


def _split_basis(
    self: qp.electrons.Wavefunction,
) -> qp.utils.Waitable[qp.electrons.Wavefunction]:
    """Return wavefunction split by basis, bringing all bands of each basis
    coefficient together on some process. Note that the result may be a view if
    there is only one process, or if the wavefunction is already split by basis
    """
    if (self.basis.division.n_procs == 1) or (self.band_division is None):
        return qp.utils.Waitless(self)  # already in required configuration
    basis = self.basis

    # Split basis dimension to proc and basis-each, bring proc dimension
    # outermost for contiguous send chunks and band dim right after
    # --- after this, dim order is (proc, band, spin, k, spinor, basis-each)
    send_coeff = (
        self.coeff.view(
            self.coeff.shape[:-1] + (basis.division.n_procs, basis.division.n_each)
        )
        .permute(4, 2, 0, 1, 3, 5)
        .contiguous()
    )
    n_per_band = np.prod(send_coeff.shape[2:])

    # All-to-all MPI rearrangement:
    band_division = self.band_division
    send_counts = band_division.n_mine * n_per_band
    send_offset = np.arange(band_division.n_procs) * send_counts
    recv_counts = np.diff(band_division.n_prev) * n_per_band
    recv_offset = band_division.n_prev[:-1] * n_per_band
    mpi_type = qp.rc.mpi_type[send_coeff.dtype]
    recv_coeff = torch.zeros(
        (band_division.n_tot,) + send_coeff.shape[2:],
        dtype=send_coeff.dtype,
        device=send_coeff.device,
    )
    qp.rc.current_stream_synchronize()
    request = basis.comm.Ialltoallv(
        (qp.utils.BufferView(send_coeff), send_counts, send_offset, mpi_type),
        (qp.utils.BufferView(recv_coeff), recv_counts, recv_offset, mpi_type),
    )
    return SplitBasisWait(request, send_coeff, recv_coeff, basis)


@dataclass
class SplitBasisWait:
    """`Waitable` object for the result of `Wavefunction.split_basis`."""

    request: MPI.Request
    send_coeff: torch.Tensor
    recv_coeff: torch.Tensor
    basis: qp.electrons.Basis

    def wait(self) -> qp.electrons.Wavefunction:
        """Complete `Wavefunction.split_basis` after waiting on MPI transfers."""
        # Wait for MPI completion:
        self.request.Wait()
        del self.send_coeff
        # Move band index into correct position (already together):
        # --- before this data order is (band, spin, k, spinor, basis)
        result = qp.electrons.Wavefunction(
            self.basis, coeff=self.recv_coeff.permute(1, 2, 0, 3, 4)
        )
        del self.recv_coeff
        return result
