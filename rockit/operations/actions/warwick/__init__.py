#
# This file is part of the Robotic Observatory Control Kit (rockit)
#
# rockit is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# rockit is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with rockit.  If not, see <http://www.gnu.org/licenses/>.

"""Actions that can be scheduled for automated observation"""

from .acquire_dark_frames import AcquireDarkFrames
from .autofocus import AutoFocus
from .focus_sweep import FocusSweep
from .initialize_camera import InitializeCamera
from .observe_image_sequence import ObserveImageSequence
from .observe_time_series import ObserveTimeSeries
from .park_telescope import ParkTelescope
from .shutdown_camera import ShutdownCamera
from .skyflats import SkyFlats
from .sync_pointing import SyncPointing
