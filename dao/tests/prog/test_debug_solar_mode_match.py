"""
Tests for the prefer_measured match on recorded solar predictions.

The debug harness records every ``predict_solar_device()`` call per device, in
call order. ``prefer_measured`` swaps the forecast irradiance for the measured
one, so a replayed call has to ask for the same weather source as the recorded
call it is served from; otherwise the replay fails instead of handing back a
prediction from the other source.
"""

import pandas as pd
import pytest

from dao.prog.da_debug import SnapshotMiss, _CallSequence, _sequence_record


def _sequence(*modes):
    frame = pd.DataFrame({"prediction": [1.0]})
    records = [
        _sequence_record({"start": "a", "end": "b", "prefer_measured": mode}, frame)
        for mode in modes
    ]
    return _CallSequence("solar prediction 'Zuid'", "selftest", records)


@pytest.mark.parametrize("mode", [False, True])
def test_the_same_mode_is_served(mode):
    assert _sequence(mode).serve(start="x", end="y", prefer_measured=mode)["prediction"].tolist() == [1.0]


@pytest.mark.parametrize(("recorded", "requested"), [(False, True), (True, False)])
def test_a_differing_mode_is_refused(recorded, requested):
    with pytest.raises(SnapshotMiss, match="prefer_measured"):
        _sequence(recorded).serve(start="x", end="y", prefer_measured=requested)


def test_a_recording_without_the_mode_is_served():
    """Snapshots captured before the mode was recorded have nothing to check."""
    frame = pd.DataFrame({"prediction": [1.0]})
    sequence = _CallSequence(
        "solar prediction 'Zuid'", "selftest", [_sequence_record({"start": "a"}, frame)]
    )
    assert len(sequence.serve(start="x", prefer_measured=True)) == 1
