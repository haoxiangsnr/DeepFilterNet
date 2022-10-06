from typing import Final, List

import torch
import torch.nn.functional as F
from torch import Tensor, nn


def as_windowed(x: Tensor, window_length: int, step: int = 1, dim: int = 1) -> Tensor:
    """Returns a tensor with chunks of overlapping windows of the first dim of x.

    Args:
        x (Tensor): Input of shape [B, T, ...]
        window_length (int): Length of each window
        step (int): Step/hop of each window w.r.t. the original signal x
        dim (int): Dimension on to apply the windowing

    Returns:
        windowed tensor (Tensor): Output tensor with shape (if dim==1)
            [B, (N - window_length + step) // step, window_length, ...]
    """
    shape: List[int] = list(x.shape)
    stride: List[int] = list(x.stride())
    # stride: List[int] = [x.stride(i) for i in range(len(shape))]
    # shape[dim] = torch.div(shape[dim] - window_length + step, step, rounding_mode="trunc")
    shape[dim] = int(shape[dim] - window_length + step / step)
    if dim > 0:
        shape.insert(dim + 1, window_length)
        stride.insert(dim + 1, stride[dim])
    else:
        if dim == -1:
            shape.append(window_length)
            stride.append(stride[dim])
        else:
            shape.insert(dim, window_length)
            stride.insert(dim, stride[dim])
    stride[dim] = stride[dim] * step
    return x.as_strided(shape, stride)


class MultiFrameModule(nn.Module):
    """Multi-frame speech enhancement modules.

    Signal model and notation:
        Noisy: `x = s + n`
        Enhanced: `y = f(x)`
        Objective: `min ||s - y||`

        PSD: Power spectral density, notated eg. as `Rxx` for noisy PSD.
        IFC: Inter-frame correlation vector: PSD*u, u: selection vector. Notated as `rxx`
        RTF: Relative transfere function, also called steering vector.
    """

    num_freqs: Final[int]
    frame_size: Final[int]
    need_unfold: Final[bool]
    real: Final[bool]

    def __init__(self, num_freqs: int, frame_size: int, lookahead: int = 0, real: bool = False):
        """Multi-Frame filtering module.

        Args:
            num_freqs (int): Number of frequency bins used for filtering.
            frame_size (int): Frame size in FD domain.
            lookahead (int): Lookahead, may be used to select the output time step. Note: This
                module does not add additional padding according to lookahead!
        """
        super().__init__()
        self.num_freqs = num_freqs
        self.frame_size = frame_size
        self.real = real
        if real:
            self.pad = nn.ConstantPad3d((0, 0, 0, 0, frame_size - 1 - lookahead, lookahead), 0.0)
        else:
            self.pad = nn.ConstantPad2d((0, 0, frame_size - 1 - lookahead, lookahead), 0.0)
        self.need_unfold = frame_size > 1
        self.lookahead = lookahead

    def spec_unfold_real(self, spec: Tensor):
        if self.need_unfold:
            spec = self.pad(spec).unfold(-3, self.frame_size, 1)
            return spec.permute(0, 1, 5, 2, 3, 4)
            # return as_windowed(self.pad(spec), self.frame_size, 1, dim=-3)
        return spec.unsqueeze(-1)

    def spec_unfold(self, spec: Tensor):
        """Pads and unfolds the spectrogram according to frame_size.

        Args:
            spec (complex Tensor): Spectrogram of shape [B, C, T, F]
        Returns:
            spec (Tensor): Unfolded spectrogram of shape [B, C, T, F, N], where N: frame_size.
        """
        if self.need_unfold:
            return self.pad(spec).unfold(2, self.frame_size, 1)
        return spec.unsqueeze(-1)

    @staticmethod
    def solve(Rxx, rss, diag_eps: float = 1e-8, eps: float = 1e-7) -> Tensor:
        return torch.einsum(
            "...nm,...m->...n", torch.inverse(_tik_reg(Rxx, diag_eps, eps)), rss
        )  # [T, F, N]

    @staticmethod
    def apply_coefs(spec: Tensor, coefs: Tensor) -> Tensor:
        # spec: [B, C, T, F, N]
        # coefs: [B, C, T, F, N]
        return torch.einsum("...n,...n->...", spec, coefs)


def psd(x: Tensor, n: int) -> Tensor:
    """Compute the PSD correlation matrix Rxx for a spectrogram.

    That is, `X*conj(X)`, where `*` is the outer product.

    Args:
        x (complex Tensor): Spectrogram of shape [B, C, T, F]. Will be unfolded with `n` steps over
            the time axis.

    Returns:
        Rxx (complex Tensor): Correlation matrix of shape [B, C, T, F, N, N]
    """
    x = F.pad(x, (0, 0, n - 1, 0)).unfold(-2, n, 1)
    return torch.einsum("...n,...m->...mn", x, x.conj())


def df(spec: Tensor, coefs: Tensor) -> Tensor:
    """Deep filter implemenation using `torch.einsum`. Requires unfolded spectrogram.

    Args:
        spec (complex Tensor): Spectrogram of shape [B, C, T, F, N]
        coefs (complex Tensor): Coefficients of shape [B, C, N, T, F]

    Returns:
        spec (complex Tensor): Spectrogram of shape [B, C, T, F]
    """
    return torch.einsum("...tfn,...ntf->...tf", spec, coefs)


def df_real(spec: Tensor, coefs: Tensor) -> Tensor:
    """Deep filter implemenation for real valued input Tensors. Requires unfolded spectrograms.

    Args:
        spec (real-valued Tensor): Spectrogram of shape [B, C, N, T, F, 2].
        coefs (real-valued Tensor): Coefficients of shape [B, C, N, T, F, 2].

    Returns:
        spec (real-valued Tensor): Filtered Spectrogram of shape [B, C, T, F, 2]
    """
    b, c, _, t, f, _ = spec.shape
    out = torch.empty((b, c, t, f, 2), dtype=spec.dtype, device=spec.device)
    # real
    out[..., 0] = (spec[..., 0] * coefs[..., 0]).sum(dim=2)
    out[..., 0] -= (spec[..., 1] * coefs[..., 1]).sum(dim=2)
    # imag
    out[..., 1] = (spec[..., 0] * coefs[..., 1]).sum(dim=2)
    out[..., 1] += (spec[..., 1] * coefs[..., 0]).sum(dim=2)
    return out


class DF(MultiFrameModule):
    """Deep Filtering."""

    conj: Final[bool]

    def __init__(self, num_freqs: int, frame_size: int, lookahead: int = 0, conj: bool = False):
        super().__init__(num_freqs, frame_size, lookahead)
        self.conj = conj

    def forward(self, spec: Tensor, coefs: Tensor):
        spec_u = self.spec_unfold(torch.view_as_complex(spec))
        coefs = torch.view_as_complex(coefs)
        spec_f = spec_u.narrow(-2, 0, self.num_freqs)
        coefs = coefs.view(coefs.shape[0], -1, self.frame_size, *coefs.shape[2:])
        if self.conj:
            coefs = coefs.conj()
        spec_f = df(spec_f, coefs)
        if self.training:
            spec = spec.clone()
        spec[..., : self.num_freqs, :] = torch.view_as_real(spec_f)
        return spec


class DFreal(MultiFrameModule):
    """Deep Filtering."""

    conj: Final[bool]

    def __init__(self, num_freqs: int, frame_size: int, lookahead: int = 0, conj: bool = False):
        super().__init__(num_freqs, frame_size, lookahead, real=True)
        self.conj = conj

    def forward(self, spec: Tensor, coefs: Tensor):
        """Pads and unfolds the spectrogram and applies deep filtering using only real valued types.

        Args:
            spec (Tensor): Spectrogram of shape [B, C, T, F, 2]
            coefs (Tensor): Spectrogram of shape [B, C, T, F, 2]
        """
        spec_u = self.spec_unfold_real(spec)
        spec_f = spec_u.narrow(-2, 0, self.num_freqs)
        new_shape = [coefs.shape[0], -1, self.frame_size] + list(coefs.shape[2:])
        coefs = coefs.view(new_shape)
        if self.conj:
            coefs = coefs.conj()
        spec_f = df_real(spec_f, coefs)
        spec[..., : self.num_freqs, :] = spec_f
        return spec


class CRM(MultiFrameModule):
    """Complex ratio mask."""

    def __init__(self, num_freqs: int, frame_size: int = 1, lookahead: int = 0):
        assert frame_size == 1 and lookahead == 0, (frame_size, lookahead)
        super().__init__(num_freqs, 1)

    def forward_impl(self, spec: Tensor, coefs: Tensor):
        return spec.squeeze(-1).mul(coefs)


class MfWf(MultiFrameModule):
    """Multi-frame Wiener filter base module."""

    def __init__(self, num_freqs: int, frame_size: int, lookahead: int = 0):
        """Multi-frame Wiener Filter via an estimate of the inverse"""
        super().__init__(num_freqs, frame_size, lookahead=lookahead)

    def forward(self, spec: Tensor, ifc: Tensor, iRxx: Tensor) -> Tensor:
        """Multi-frame Wiener filter based on Rxx**-1 and speech IFC vector.

        Args:
            spec (complex Tensor): Spectrogram of shape [B, 1, T, F]
            ifc (complex Tensor): Inter-frame speech correlation vector [B, T, F, N*2]
            iRxx (complex Tensor): Inverse noisy covariance matrix Rxx**-1 [B, T, F, (N**2)*2]

        Returns:
            spec (complex Tensor): Filtered spectrogram of shape [B, C, T, F]
        """
        spec_u = self.spec_unfold(torch.view_as_complex(spec))
        iRxx = torch.view_as_complex(iRxx.unflatten(3, (self.frame_size, self.frame_size, 2)))
        ifc = torch.view_as_complex(ifc.unflatten(3, (self.frame_size, 2)))
        spec_f = spec_u.narrow(-2, 0, self.num_freqs)
        w = torch.einsum("...nm,...m->...n", iRxx, ifc)  # [B, C, F, N]
        spec_f = self.apply_coefs(spec_f, w)
        if self.training:
            spec = spec.clone()
        spec[..., : self.num_freqs, :] = torch.view_as_real(spec_f)
        return spec


class MfMvdr(MultiFrameModule):
    """Multi-frame minimum variance distortionless beamformer based on Rnn**-1 and speech IFC vector."""

    def __init__(self, num_freqs: int, frame_size: int, lookahead: int = 0, eps: float = 1e-8):
        """Multi-frame minimum variance distortionless beamformer."""
        super().__init__(num_freqs, frame_size, lookahead=lookahead)
        self.eps = eps

    def forward(self, spec: Tensor, ifc: Tensor, iRnn: Tensor) -> Tensor:
        """Multi-frame MVDR filter based on Rnn**-1 and speech IFC vector.

        Args:
            spec (complex Tensor): Spectrogram of shape [B, C, T, F]
            ifc (complex Tensor): Inter-frame speech correlation vector [B, C*N*2, T, F]
            iRnn (complex Tensor): Inverse noise covariance matrix Rnn**-1 [B, C*(N**2)*2, T, F]

        Returns:
            spec (complex Tensor): Filtered spectrogram of shape [B, C, T, F]
        """
        spec_u = self.spec_unfold(torch.view_as_complex(spec))
        iRnn = torch.view_as_complex(iRnn.unflatten(3, (self.frame_size, self.frame_size, 2)))
        ifc = torch.view_as_complex(ifc.unflatten(3, (self.frame_size, 2)))
        spec_f = spec_u.narrow(-2, 0, self.num_freqs)
        numerator = torch.einsum("...nm,...m->...n", iRnn, ifc)  # [B, C, F, N]
        denumerator = torch.einsum("...n,...n->...", ifc.conj(), numerator)
        w = numerator / (denumerator.real.unsqueeze(-1) + self.eps)
        spec_f = self.apply_coefs(spec_f, w)
        if self.training:
            spec = spec.clone()
        spec[..., : self.num_freqs, :] = torch.view_as_real(spec_f)
        return spec


# From torchaudio
def _compute_mat_trace(input: torch.Tensor, dim1: int = -1, dim2: int = -2) -> torch.Tensor:
    r"""Compute the trace of a Tensor along ``dim1`` and ``dim2`` dimensions.
    Args:
        input (torch.Tensor): Tensor of dimension `(..., channel, channel)`
        dim1 (int, optional): the first dimension of the diagonal matrix
            (Default: -1)
        dim2 (int, optional): the second dimension of the diagonal matrix
            (Default: -2)
    Returns:
        Tensor: trace of the input Tensor
    """
    assert input.ndim >= 2, "The dimension of the tensor must be at least 2."
    assert (
        input.shape[dim1] == input.shape[dim2]
    ), "The size of ``dim1`` and ``dim2`` must be the same."
    input = torch.diagonal(input, 0, dim1=dim1, dim2=dim2)
    return input.sum(dim=-1)


def _tik_reg(mat: torch.Tensor, reg: float = 1e-7, eps: float = 1e-8) -> torch.Tensor:
    """Perform Tikhonov regularization (only modifying real part).
    Args:
        mat (torch.Tensor): input matrix (..., channel, channel)
        reg (float, optional): regularization factor (Default: 1e-8)
        eps (float, optional): a value to avoid the correlation matrix is all-zero (Default: ``1e-8``)
    Returns:
        Tensor: regularized matrix (..., channel, channel)
    """
    # Add eps
    C = mat.size(-1)
    eye = torch.eye(C, dtype=mat.dtype, device=mat.device)
    epsilon = _compute_mat_trace(mat).real[..., None, None] * reg
    # in case that correlation_matrix is all-zero
    epsilon = epsilon + eps
    mat = mat + epsilon * eye[..., :, :]
    return mat
