from __future__ import annotations
import qimpy as qp
import numpy as np
import torch
from ._basis_ops import _apply_ke, _apply_potential, _collect_density
from ._basis_real import BasisReal
from typing import Optional, Tuple, Union


class Basis(qp.Constructable):
    """Plane-wave basis for electronic wavefunctions. The underlying
     :class:`qimpy.utils.TaskDivision` splits plane waves over `rc.comm_b`"""
    __slots__ = ('lattice', 'ions', 'kpoints', 'n_spins', 'n_spinor',
                 'k', 'wk', 'w_sk', 'real_wavefunctions', 'ke_cutoff', 'grid',
                 'iG', 'n', 'n_min', 'n_max', 'n_avg', 'n_tot', 'n_ideal',
                 'fft_index', 'fft_block_size', 'pad_index', 'pad_index_mine',
                 'division', 'mine', 'real')
    lattice: qp.lattice.Lattice  #: Lattice vectors of unit cell
    ions: qp.ions.Ions  #: Ionic system: implicit in basis for ultrasoft / PAW
    kpoints: qp.electrons.Kpoints  #: Corresponding k-point set
    n_spins: int  #: Default number of spin channels
    n_spinor: int  #: Default number of spinorial components
    k: torch.Tensor  #: Subset of k handled by this basis (due to MPI division)
    wk: torch.Tensor  #: Subset of weights corresponding to `k`
    w_sk: torch.Tensor  #: Combined spin and k-point weights
    real_wavefunctions: bool  #: Whether wavefunctions are real
    ke_cutoff: float  #: Kinetic energy cutoff
    grid: qp.grid.Grid  #: Wavefunction grid (always process-local)
    iG: torch.Tensor  #: Plane waves in reciprocal lattice coordinates
    n: torch.Tensor  #: Number of plane waves for each `k`
    n_min: int  #: Minimum of `n` across all `k` (including on other processes)
    n_max: int  #: Maximum of `n` across all `k` (including on other processes)
    n_avg: float  #: Average `n` across all `k` (weighted by `wk`)
    n_tot: int  #: Actual common `n` stored for each `k` including padding
    n_ideal: float  #: Ideal `n_avg` based on `ke_cutoff` G-sphere volume
    fft_index: torch.Tensor  #: Index of each plane wave in reciprocal grid
    fft_block_size: int  #: Number of bands to FFT together
    PadIndex = Tuple[slice, torch.Tensor, slice, slice, torch.Tensor] \
        #: Indexing datatype for `pad_index` and `pad_index_mine`
    pad_index: PadIndex  #: Which basis entries are padding (beyond `n`)
    pad_index_mine: PadIndex  #: Subset of `pad_index` on this process
    division: qp.utils.TaskDivision  #: Division of basis across `rc.comm_b`
    mine: slice  #: Slice of basis entries local to this process
    real: BasisReal  #: Extra indices for real wavefunctions

    apply_ke = _apply_ke
    apply_potential = _apply_potential
    collect_density = _collect_density

    def __init__(self, *, co: qp.ConstructOptions,
                 lattice: qp.lattice.Lattice, ions: qp.ions.Ions,
                 symmetries: qp.symmetries.Symmetries,
                 kpoints: qp.electrons.Kpoints, n_spins: int, n_spinor: int,
                 ke_cutoff: float = 20., real_wavefunctions: bool = False,
                 grid: Optional[dict] = None, fft_block_size: int = 0) -> None:
        """Initialize plane-wave basis with `ke_cutoff`.

        Parameters
        ----------
        lattice
            Lattice whose reciprocal lattice vectors define plane-wave basis.
        ions
            Ions that specify the pseudopotential portion of the basis;
            the basis implicitly depends on the ion positions for ultrasoft
            or PAW due to the augmentation of all operators at each ion.
        symmetries
            Symmetries with which the wavefunction grid should be commensurate.
        kpoints
            Set of k-points to initialize basis for. Note that the basis is
            only initialized for k-points to be operated on by current process
            i.e. for k = kpoints.k[kpoints.i_start : kpoints.i_stop].
        n_spins
            Default number of spin channels for wavefunctions in this basis.
        n_spinor
            Default number of spinor components for wavefunctions in this
            basis. Also used only to check support for real_wavefunctions.
        ke_cutoff
            :yaml:`Wavefunction kinetic energy cutoff in Hartrees.`
        real_wavefunctions
            :yaml:`Whether to use real wavefunctions (instead of complex).`
            This is only supported for non-spinorial, Gamma-point-only
            calculations, where conjugate symmetry allows real wavefunctions.
        grid
            :yaml:`Override parameters of grid for wavefunction operations.`
            Specify a dict with
            (such as `shape` or
            `ke_cutoff`)  :yaml:`inputfile`
        fft_block_size
            Number of wavefunction bands to FFT simultaneously.
            Higher numbers require more memory, but can achieve
            better occupancy of GPUs or high-core-count CPUs.
            The default of 0 auto-selects the block size based on the number
            of bands and k-points being processed by each process.
            :yaml:`inputfile`
        """
        super().__init__(co=co)
        rc = self.rc
        self.lattice = lattice
        self.ions = ions
        self.kpoints = kpoints
        self.n_spins = n_spins
        self.n_spinor = n_spinor
        self.fft_block_size = int(fft_block_size)

        # Select subset of k-points relevant on this process:
        k_mine = slice(kpoints.division.i_start, kpoints.division.i_stop)
        self.k = kpoints.k[k_mine]
        self.wk = kpoints.wk[k_mine]
        w_spin = 2 // (self.n_spins * self.n_spinor)  # spin weight
        self.w_sk = w_spin * self.wk.view(1, -1, 1)  # combined k and spin

        # Check real wavefunction support:
        self.real_wavefunctions = real_wavefunctions
        if self.real_wavefunctions:
            if n_spinor == 2:
                raise ValueError('real-wavefunctions not compatible'
                                 ' with spinorial calculations')
            if kpoints.k.norm().item():  # i.e. not all k = 0 (Gamma)
                raise ValueError('real-wavefunctions only compatible with'
                                 ' Gamma-point-only calculations')

        # Initialize grid to match cutoff:
        self.ke_cutoff = float(ke_cutoff)
        qp.log.info('\nInitializing wavefunction grid:')
        self.construct('grid', qp.grid.Grid, grid, lattice=lattice,
                       symmetries=symmetries, comm=None,  # Never parallel
                       ke_cutoff_wavefunction=self.ke_cutoff)

        # Initialize basis:
        self.iG = self.grid.get_mesh('H' if self.real_wavefunctions
                                     else 'G').view(-1, 3)
        within_cutoff = (self.get_ke() < ke_cutoff)  # mask of which iG to keep
        # --- determine statistics of basis count across all k:
        self.n = within_cutoff.count_nonzero(dim=1)
        self.n_min = qp.utils.globalreduce.min(self.n, rc.comm_k)
        self.n_max = qp.utils.globalreduce.max(self.n, rc.comm_k)
        self.n_tot = qp.utils.ceildiv(self.n_max, rc.n_procs_b) * rc.n_procs_b
        self.n_avg = qp.utils.globalreduce.sum(self.n * self.wk, rc.comm_k)
        self.n_ideal = ((2.*ke_cutoff)**1.5) * lattice.volume / (6 * np.pi**2)
        qp.log.info(f'n_basis:  min: {self.n_min}  max: {self.n_max}'
                    f'  avg: {self.n_avg:.3f}  ideal: {self.n_ideal:.3f}')
        # --- create indices from basis set to FFT grid:
        n_fft = self.iG.shape[0]  # number of points on FFT grid
        assert(self.n_tot <= n_fft)  # make sure padding doesn't exceed grid
        fft_range = torch.arange(n_fft, device=self.rc.device)
        self.fft_index = (torch.where(within_cutoff, 0, n_fft)
                          + fft_range[None, :]).argsort(  # ke<cutoff to front
                              dim=1)[:, :self.n_tot]  # same count all k
        self.iG = self.iG[self.fft_index]  # basis plane waves for each k
        pad_index = torch.where(fft_range[None, :self.n_tot]
                                >= self.n[:, None])  # padded entries
        self.pad_index = (slice(None), pad_index[0], slice(None), slice(None),
                          pad_index[1])  # add spin, band and spinor dims

        # Divide basis on comm_b:
        div = qp.utils.TaskDivision(n_tot=self.n_tot, n_procs=rc.n_procs_b,
                                    i_proc=rc.i_proc_b, name='padded basis')
        self.division = div
        self.mine = slice(div.i_start, div.i_stop)
        # --- initialize local pad index separately (not trivially sliceable):
        pad_index = torch.where(fft_range[None, div.i_start:div.i_stop]
                                >= self.n[:, None])  # local padded entries
        self.pad_index_mine = (slice(None), pad_index[0], slice(None),
                               slice(None), pad_index[1])  # add other dims

        if self.real_wavefunctions and kpoints.division.n_mine:
            self.real = BasisReal(self)

    def get_ke(self, basis_slice: slice = slice(None)) -> torch.Tensor:
        """Kinetic energy (KE) of each plane wave in basis in :math:`E_h`

        Parameters
        ----------
        basis_slice
            Selection of basis functions to get KE for (default: full basis)

        Returns
        -------
        torch.Tensor
            KE for each plane-wave, dimensions: `nk_mine` x len(`basis_slice`)
        """
        return 0.5 * (((self.iG[:, basis_slice] + self.k[:, None, :])
                       @ self.lattice.Gbasis.T) ** 2).sum(dim=-1)

    def get_fft_block_size(self, n_batch: int, n_bands: int) -> int:
        """Number of FFTs to perform together. Equals `fft_block_size`, if
        that is non-zero, and uses a heuristic based on batch dimension
        and number of bands, if manually specified."""
        if self.fft_block_size:
            return self.fft_block_size
        else:
            if not (n_batch and n_bands):
                return 1  # Irrelevant since no FFTs to perform anyway
            # TODO: better heuristics on how much data to FFT at once:
            min_data = 16_000_000 if self.rc.use_cuda else 100_000
            min_block = qp.utils.ceildiv(min_data,
                                         n_batch * np.prod(self.grid.shape))
            max_block = qp.utils.ceildiv(n_bands, 16)  # based on memory limit
            return min(min_block, max_block)
