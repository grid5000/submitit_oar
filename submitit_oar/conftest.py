# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
#

import time

import pytest


@pytest.fixture()
def fast_forward_clock(monkeypatch):
    """Allows to go in the future."""
    clock_time = [time.time()]

    monkeypatch.setattr(time, "time", lambda: clock_time[0])

    def _fast_forward(minutes: float):
        clock_time[0] += minutes * 60

    return _fast_forward
