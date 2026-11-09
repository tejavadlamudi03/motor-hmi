import math
import random

import pytest

from moteus_hmi.core.filters import (ButterworthLowPass, EmaFilter, HampelFilter, Kalman1D,
                                     MedianFilter, NotchFilter, adapt_chain, build_chain,
                                     detrended_std, validate_chain)

FS = 100.0
DT = 1.0 / FS


def run(f, xs):
    return [f.update(x, DT) for x in xs]


@pytest.mark.parametrize("f", [EmaFilter(5), ButterworthLowPass(5, FS), NotchFilter(17, 4, FS),
                               Kalman1D(1.0, 0.01), MedianFilter(5), HampelFilter(7, 3)])
def test_constant_input_has_no_startup_transient(f):
    out = run(f, [3.0] * 50)
    assert all(abs(y - 3.0) < 1e-9 for y in out)


@pytest.mark.parametrize("f", [EmaFilter(5), ButterworthLowPass(5, FS), Kalman1D(1.0, 0.01)])
def test_nan_passes_through_without_corrupting_state(f):
    run(f, [1.0] * 20)
    assert math.isnan(f.update(math.nan, DT))
    assert abs(f.update(1.0, DT) - 1.0) < 1e-6


def test_butterworth_attenuates_high_frequency_noise():
    rng = random.Random(1)
    xs = [rng.gauss(0, 1) for _ in range(4000)]
    ys = run(ButterworthLowPass(5, FS), xs)
    raw, filt = detrended_std(xs), detrended_std(ys[100:])
    assert filt < raw / 5


def test_notch_removes_tone():
    xs = [math.sin(2 * math.pi * 17 * i * DT) for i in range(3000)]
    ys = run(NotchFilter(17, 2, FS), xs)
    assert max(abs(y) for y in ys[1000:]) < 0.05


def test_hampel_rejects_spike_and_keeps_edges():
    xs = [0.0] * 20 + [10.0] + [0.0] * 20 + [1.0] * 20
    f = HampelFilter(7, 3)
    ys = run(f, [x + 1e-3 * (i % 3) for i, x in enumerate(xs)])
    assert max(ys[18:25]) < 0.1          # spike removed
    assert abs(ys[-1] - 1.0) < 0.01      # step preserved
    assert f.rejected >= 1


def test_median_rejects_single_spike():
    ys = run(MedianFilter(5), [0, 0, 0, 9, 0, 0, 0])
    assert max(ys) == 0


def test_invalid_specs_rejected():
    assert validate_chain([{"type": "butterworth", "cutoff_hz": 60}], FS)  # above Nyquist
    assert validate_chain([{"type": "median", "window": 4}], FS)
    assert validate_chain([{"type": "bogus"}], FS)
    assert validate_chain([{"type": "butterworth", "cutoff_hz": 10}], FS) is None


def test_adapt_chain_for_slow_links():
    specs = adapt_chain([{"type": "butterworth", "cutoff_hz": 15}, {"type": "notch", "center_hz": 17}], 20.0)
    assert specs == [{"type": "butterworth", "cutoff_hz": 8.0}]
    build_chain(specs, 20.0)
