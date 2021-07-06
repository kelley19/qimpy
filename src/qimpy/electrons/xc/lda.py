from .functional import Functional
import numpy as np
import torch
from abc import abstractmethod


class KE_TF(Functional):
    """Thomas-Fermi kinetic energy functional."""
    def __init__(self, scale_factor: float = 1.) -> None:
        super().__init__(has_kinetic=True, scale_factor=scale_factor)

    def __call__(self, n: torch.Tensor, sigma: torch.Tensor,
                 lap: torch.Tensor, tau: torch.Tensor) -> float:
        n_spins = n.shape[0]
        prefactor = (0.3 * ((3*(np.pi**2) * n_spins) ** (2./3.))
                     * self.scale_factor)
        n.requires_grad_()
        E = prefactor * (n ** (5./3)).sum()
        E.backward()  # updates n.grad
        return E.item()


class X_Slater(Functional):
    """Slater exchange functional."""
    def __init__(self, scale_factor: float = 1.) -> None:
        super().__init__(has_exchange=True, scale_factor=scale_factor)

    def __call__(self, n: torch.Tensor, sigma: torch.Tensor,
                 lap: torch.Tensor, tau: torch.Tensor) -> float:
        n_spins = n.shape[0]
        prefactor = -0.75 * ((3*n_spins/np.pi) ** (1./3.)) * self.scale_factor
        n.requires_grad_()
        E = prefactor * (n ** (4./3)).sum()
        E.backward()  # updates n.grad
        return E.item()


class SpinInterpolated(Functional):
    """Abstract base class for spin-interpolated LDA functionals.
    This is typical for most LDA correlation functionals."""
    __slots__ = ('stiffness_scale',)
    stiffness_scale: float  #: scale factor for spin-stiffness term

    def __init__(self, **kwargs):
        super().__init__(**kwargs)  # Forward remaining arguments
        self.stiffness_scale = (9./4) * (2.**(1./3) - 1)  # overridden in PW

    def __call__(self, n: torch.Tensor, sigma: torch.Tensor,
                 lap: torch.Tensor, tau: torch.Tensor) -> float:
        n_spins = n.shape[0]
        n.requires_grad_()
        n_tot = n.sum(dim=0)
        rs = ((4.*np.pi/3.) * n_tot) ** (-1./3)
        ec_spins = self.compute(rs, (n_spins == 2))
        # Interpolate between spin channels:
        n_channels = ec_spins.shape[-1]
        if n_channels == 1:  # un-polarized: no spin interpolation needed
            ec = ec_spins[..., 0]
        else:
            zeta = (n[0] - n[1]) / n_tot
            spin_interp = (((1 + zeta)**(4./3) + (1 - zeta)**(4./3) - 2.)
                           / (2.**(4./3) - 2.))
            if n_channels == 2:  # interpolate between para and ferro
                ec_para, ec_ferro = ec_spins.unbind(dim=-1)
                ec = ec_para + spin_interp * (ec_ferro - ec_para)
            else:  # n_channels == 3: additionally include spin stiffness
                ec_para, ec_ferro, ec_stiff = ec_spins.unbind(dim=-1)
                zeta4 = zeta ** 4
                w1 = zeta4 * spin_interp
                w2 = (zeta4 - 1.) * spin_interp * self.stiffness_scale
                ec = ec_para + w1 * (ec_ferro - ec_para) + w2 * ec_stiff
        # Compute energy density:
        E = (n_tot * ec).sum() * self.scale_factor
        E.backward()  # updates n.grad
        return E.item()

    @abstractmethod
    def compute(self, rs: torch.Tensor, spin_polarized: bool) -> torch.Tensor:
        """Compute energy (per-particle) to be spin-interpolated.
        Output should have one extra dimension at the end beyond those of `rs`
        containing various channels to be spin-interpolated. This dimension
        should be of length 1 when `spin_polarized` is False, and of length
        2 or 3 when `spin_polarized` is True. The spin channels correspond
        to paramagnetic, ferromagnetic and optionally spin-stiffness."""


class C_PZ(SpinInterpolated):
    """Perdew-Zunger LDA correlation functional."""
    __slots__ = ('_params',)
    _params: torch.Tensor  # PZ functional parameters

    def __init__(self, scale_factor: float = 1.) -> None:
        super().__init__(has_correlation=True, scale_factor=scale_factor)
        self._params = torch.tensor([
            [0.0311, 0.01555],  # a
            [-0.0480, -0.0269],  # b
            [0.0020, 0.0007],  # c
            [-0.0116, -0.0048],  # d
            [-0.1423, -0.0843],  # gamma
            [1.0529, 1.3981],  # beta1
            [0.3334, 0.2611],  # beta2
        ])

    def compute(self, rs: torch.Tensor, spin_polarized: bool) -> torch.Tensor:
        n_channels = (2 if spin_polarized else 1)
        _params = self._params[:, :n_channels].to(rs.device)
        a, b, c, d, gamma, beta1, beta2 = _params.unbind()
        e = torch.empty(rs.shape + (n_channels,))  # energy density
        # --- rs < 1 case:
        sel = torch.where(rs < 1.)
        if len(sel[0]):
            rs_sel = rs[sel][..., None]  # add dim for spin interpolation
            e[sel] = (a + c*rs_sel) * torch.log(rs_sel) + b + d*rs_sel
        # --- rs >= 1 case:
        sel = torch.where(rs >= 1.)
        if len(sel[0]):
            rs_sel = rs[sel][..., None]  # add dim for spin interpolation
            e[sel] = gamma / (1. + beta1*rs_sel.sqrt() + beta2*rs_sel)
        return e


class C_VWN(SpinInterpolated):
    """Vosko-Wilk-Nusair LDA correlation functional."""
    __slots__ = ('_params',)
    _params: torch.Tensor  # VWN functional parameters

    def __init__(self, scale_factor: float = 1.) -> None:
        super().__init__(has_correlation=True, scale_factor=scale_factor)
        self._params = torch.tensor([
            [0.0310907, 0.01554535,  1./(6.*(np.pi**2))],  # A
            [3.72744, 7.06042, 1.13107],  # b
            [12.9352, 18.0578, 13.0045],  # c
            [-0.10498, -0.32500, -0.0047584],  # x0
        ])

    def compute(self, rs: torch.Tensor, spin_polarized: bool) -> torch.Tensor:
        n_channels = (3 if spin_polarized else 1)
        _params = self._params[:, :n_channels].to(rs.device)
        A, b, c, x0 = _params.unbind()
        # Commonly used combinations of rs:
        X0 = c + x0*(b + x0)
        Q = (4.*c - b*b).sqrt()
        x = rs.sqrt()[..., None]  # add dim for spin interpolation
        X = c + x*(b + x)
        X_x = 2*x + b
        # Three transcendental terms:
        atan_term = (2./Q) * (Q / X_x).atan()
        log_term1 = (x.square() / X).log()
        log_term2 = ((x - x0).square() / X).log()
        # Final combination to correlation energy:
        return A*(log_term1 + b*(atan_term - (x0/X0)*(log_term2
                                                      + (b+2*x0)*atan_term)))
