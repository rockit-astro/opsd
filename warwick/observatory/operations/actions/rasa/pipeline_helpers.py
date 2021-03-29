#
# This file is part of opsd.
#
# opsd is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# opsd is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with opsd.  If not, see <http://www.gnu.org/licenses/>.

"""Helper functions for actions to interact with the pipeline"""

# pylint: disable=broad-except
# pylint: disable=invalid-name
# pylint: disable=too-many-return-statements
# pylint: disable=too-few-public-methods
# pylint: disable=too-many-branches
# pylint: disable=too-many-statements

import sys
import traceback
import Pyro4
from warwick.observatory.common import daemons, log
from warwick.rasa.pipeline import (
    CommandStatus as PipelineCommandStatus)

def pipeline_enable_archiving(log_name, arm, enabled):
    """Toggle archiving on or off for a given arm name"""
    try:
        with daemons.rasa_pipeline.connect() as pipeline:
            return pipeline.set_archive(arm, enabled) == PipelineCommandStatus.Succeeded
    except Pyro4.errors.CommunicationError:
        print('Failed to communicate with pipeline daemon')
        log.error(log_name, 'Failed to communicate with pipeline daemon')
        return False
    except Exception:
        print('Unknown error while configuring pipeline')
        traceback.print_exc(file=sys.stdout)
        log.error(log_name, 'Unknown error while configuring pipeline')
        return False

def configure_pipeline(log_name, config, quiet=False):
    """Toggle archiving on or off for a given arm name"""
    try:
        with daemons.rasa_pipeline.connect() as pipeline:
            return pipeline.configure(config, quiet=quiet) == PipelineCommandStatus.Succeeded
    except Pyro4.errors.CommunicationError:
        print('Failed to communicate with pipeline daemon')
        log.error(log_name, 'Failed to communicate with pipeline daemon')
        return False
    except Exception:
        print('Unknown error while configuring pipeline')
        traceback.print_exc(file=sys.stdout)
        log.error(log_name, 'Unknown error while configuring pipeline')
        return False
