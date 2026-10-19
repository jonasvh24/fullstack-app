"""EBU R128 / ITU-R BS.1770-4 in numpy: integrated loudness, true peak, normalisation.

FFmpeg's loudnorm resamples to 192 kHz internally and takes two passes; on a
10-minute mix that was a minute of every refinement, longer than re-encoding the
picture. The same measurement here is a K-weighting filter, gated 400 ms blocks
and a 4x-oversampled peak: a few seconds. Normalisation is a linear gain, with a
lookahead limiter only when the gain would push the true peak over the ceiling.

Masters are still measured afterwards by FFmpeg's own ebur128 on the delivered
file, so Gate 1 never takes this module's word for its own work.
"""

from __future__ import annotations

import numpy as np
from scipy.ndimage import minimum_filter1d, uniform_filter1d
from scipy.signal import lfilter, resample_poly

# K-weighting at 48 kHz (BS.1770-4, table 1 and 2).
SHELF = ([1.53512485958697, -2.69169618940638, 1.19839281085285],
         [1.0, -1.69065929318241, 0.73248077421585])
HIGHPASS = ([1.0, -2.0, 1.0], [1.0, -1.99004745483398, 0.99007225036621])
SR = 48_000


def integrated(x: np.ndarray, sr: int = SR) -> float | None:
    """Integrated loudness in LUFS of a (samples, channels) float array; None for silence."""
    if sr != SR:
        raise ValueError("K-weighting coefficients here are for 48 kHz")
    # float32 throughout, one contiguous row per channel: ten times faster than float64,
    # and loudness to 0.01 LU.
    f32 = lambda c: [np.asarray(v, np.float32) for v in c]  # noqa: E731
    ch = np.ascontiguousarray(x.T, dtype=np.float32)
    y = lfilter(*f32(SHELF), ch, axis=1)
    y = lfilter(*f32(HIGHPASS), y, axis=1)
    hop = int(0.1 * sr)
    hops = y.shape[1] // hop
    if hops < 4:
        return None
    # A 400 ms gating block is four 100 ms hops: sum squares per hop, then add neighbours.
    per_hop = (y[:, : hops * hop] ** 2).reshape(y.shape[0], hops, hop).sum(axis=2,
                                                                            dtype=np.float64)
    blocks = per_hop[:, :-3] + per_hop[:, 1:-2] + per_hop[:, 2:-1] + per_hop[:, 3:]
    z = (blocks / (4 * hop)).T                              # mean square per block, per channel
    power = z.sum(axis=1)                                   # channel weights are 1 for L and R
    with np.errstate(divide="ignore"):
        lk = -0.691 + 10 * np.log10(power)
    above = power[lk > -70]
    if not len(above):
        return None
    rel = -0.691 + 10 * np.log10(above.mean()) - 10
    gated = power[(lk > -70) & (lk > rel)]
    return float(-0.691 + 10 * np.log10(gated.mean())) if len(gated) else None


INTERSAMPLE_HEADROOM_DB = 3.5   # a 4x-oversampled peak never exceeds the sample peak by more


def true_peak(x: np.ndarray, block_s: float = 0.02) -> float:
    """Peak of the 4x-oversampled signal in dBTP. Only blocks whose sample peak comes within
    the inter-sample headroom of the loudest sample can hold the true peak, so only those
    are oversampled: a handful of 20 ms windows instead of the whole mix."""
    n = int(block_s * SR)
    a = np.abs(x).max(axis=1)
    blocks = len(a) // n
    if blocks == 0:
        up = resample_poly(x, 4, 1, axis=0)
        return 20 * np.log10(max(float(np.abs(up).max()) if up.size else 0.0, 1e-9))
    per = a[: blocks * n].reshape(blocks, n).max(axis=1)
    tail = float(a[blocks * n:].max()) if len(a) > blocks * n else 0.0
    sample_peak = max(float(per.max()), tail)
    floor = sample_peak * 10 ** (-INTERSAMPLE_HEADROOM_DB / 20)
    idx = list(np.flatnonzero(per >= floor)) + ([blocks] if tail >= floor else [])
    peak, pad = sample_peak, 64
    for k in idx:
        a0, a1 = k * n, min(len(x), (k + 1) * n)
        lo, hi = max(0, a0 - pad), min(len(x), a1 + pad)
        up = resample_poly(x[lo:hi], 4, 1, axis=0)
        # Only the window's interior: the filter's start-up at the edges is not signal.
        inner = up[(a0 - lo) * 4:(a1 - lo) * 4]
        if inner.size:
            peak = max(peak, float(np.abs(inner).max()))
    return 20 * np.log10(max(peak, 1e-9))


def _limit(x: np.ndarray, ceiling_db: float) -> np.ndarray:
    """Lookahead peak limiter on the sample envelope: the gain each sample needs, held
    over a 5 ms window around it (a minimum filter), then smoothed so it never clicks.
    The caller leaves inter-sample headroom in the ceiling and verifies the true peak."""
    ceiling = 10 ** (ceiling_db / 20)
    env = np.abs(x).max(axis=1)
    need = np.minimum(1.0, ceiling / np.maximum(env, 1e-9))
    win = int(0.005 * SR)
    held = minimum_filter1d(need, size=2 * win + 1, mode="nearest")
    smooth = uniform_filter1d(held, size=win, mode="nearest")
    gain = np.minimum(smooth, held)          # smoothing must never let a peak through
    return (x * gain[:, None]).astype(np.float32)


def normalise(x: np.ndarray, target_lufs: float, ceiling_dbtp: float) -> tuple[np.ndarray, dict]:
    """Linear gain to the target, limited only where the true peak demands it."""
    measured = integrated(x)
    if measured is None:
        return x, {"integrated_lufs": None, "true_peak_dbtp": None, "gain_db": 0.0,
                   "limited": False}
    gain_db = target_lufs - measured
    y = (x * 10 ** (gain_db / 20)).astype(np.float32)
    tp = true_peak(y)
    limited = tp > ceiling_dbtp
    if limited:
        ceiling = ceiling_dbtp - 0.5            # room for inter-sample peaks and AAC
        y = _limit(y, ceiling)
        # Limiting took a little loudness away; give it back once, then re-limit.
        lost = target_lufs - (integrated(y) or target_lufs)
        if lost > 0.05:
            y = _limit((y * 10 ** (lost / 20)).astype(np.float32), ceiling)
        tp = true_peak(y)
        if tp > ceiling_dbtp - 0.2:             # an inter-sample peak got through: trim it
            y = _limit(y, ceiling - (tp - ceiling_dbtp + 0.2))
            tp = true_peak(y)
    # A linear gain moves loudness by exactly the gain; only a limited mix is re-measured.
    level = (integrated(y) or target_lufs) if limited else measured + gain_db
    return y, {"integrated_lufs": round(float(level), 2), "true_peak_dbtp": round(float(tp), 2),
               "gain_db": round(float(gain_db), 2), "limited": bool(limited)}
