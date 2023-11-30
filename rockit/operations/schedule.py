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

"""Helper functions for validating and parsing schedule JSON objects into actions"""

# pylint: disable=too-many-branches

import sys
import traceback
import jsonschema
from skyfield import almanac
from skyfield.api import Loader
from astropy.time import Time
import astropy.units as u
from rockit.common import validation



def __format_errors(errors):
    for error in sorted(errors, key=lambda e: e.path):
        if error.path:
            path = '->'.join([str(p) for p in error.path])
            yield path + ': ' + error.message
        else:
            yield error.message


def night_start_end(night, config):
    loader = Loader('/var/tmp/')
    ts = loader.timescale()
    eph = loader('de421.bsp')

    sun_above_horizon = almanac.risings_and_settings(eph, eph['Sun'], config.site_location)

    # Search for sunset/sunrise between midday on 'night' and midday the following day
    night = Time.strptime(night, '%Y-%m-%d') + 12 * u.hour
    night_search_start = ts.from_astropy(night)
    night_search_end = ts.from_astropy(night + 1 * u.day)
    events, _ = almanac.find_discrete(night_search_start, night_search_end, sun_above_horizon)
    return events[0].to_astropy(), events[1].to_astropy()


def __validate_dome(block, config, night):
    """Returns a list of error messages that stop json from defining a valid dome schedule"""
    try:
        night_start, night_end = night_start_end(night, config)

        # pylint: disable=unused-argument
        def require_night(validator, value, instance, schema):
            """Create a validator object that forces a tagged date to match
               the night defined in the observing plan
            """
            if instance == 'auto':
                return

            try:
                date = Time.strptime(instance, '%Y-%m-%dT%H:%M:%SZ')
            except Exception:
                yield jsonschema.ValidationError(f'{instance} is not a valid datetime')
                return

            if value and (date < night_start or date > night_end):
                start_str = night_start.strftime('%Y-%m-%dT%H:%M:%SZ')
                end_str = night_end.strftime('%Y-%m-%dT%H:%M:%SZ')
                yield jsonschema.ValidationError(f'{instance} is not auto or between {start_str} and {end_str}')

        # pylint: enable=unused-argument

        schema = {
            'type': 'object',
            'additionalProperties': False,
            'required': ['open', 'close'],
            'properties': {
                'open': {
                    'type': 'string',
                    'require-night': True
                },
                'close': {
                    'type': 'string',
                    'require-night': True
                }
            }
        }

        errors = __format_errors(validation.validation_errors(block, schema, {
            'require-night': require_night
        }))

    except Exception:
        errors = ['exception while validating']
        traceback.print_exc(file=sys.stdout)

    # Prefix each message with the action index and type
    return ['dome: ' + e for e in errors]


def __validate_action(index, block, action_types):
    """Validates an action block and returns a list of any schema violations"""
    if 'type' not in block:
        return ['action ' + str(index) + ": missing key 'type'"]

    if block['type'] not in action_types:
        return ['action ' + str(index) + ": unknown action type '" + block['type'] + "'"]

    try:
        errors = __format_errors(action_types[block['type']].validate_config(block))
    except Exception:
        errors = ['exception while validating']
        traceback.print_exc(file=sys.stdout)

    # Prefix each message with the action index and type
    return ['action ' + str(index) + ' (' + block['type'] + '): ' + e for e in errors]


def validate_schedule(json, config, require_tonight):
    """
    Tests whether a json object defines a valid opsd schedule
    Returns a tuple of (valid, messages) where:
       valid is a boolean indicating whether the schedule is valid
       messages is a list of strings describing errors in the schedule
    """

    errors = []

    # Schedule requires a night to be defined
    if 'night' not in json:
        errors.append('missing key \'night\'')
    else:
        try:
            schedule_night = Time.strptime(json['night'], '%Y-%m-%d') + 12 * u.hour
        except ValueError:
            errors.append(f'night: {json["night"]} is not a valid date')

    # Errors with 'night' are fatal
    if errors:
        return True, errors

    current_night = Time.now()
    if current_night.to_datetime().hour < 12:
        current_night -= 1 * u.day
    current_night = Time.strptime(current_night.strftime('%Y-%m-%d'), '%Y-%m-%d') + 12 * u.hour

    if 'dome' in json:
        errors.extend(__validate_dome(json['dome'], config, json['night']))

    if 'actions' in json:
        if isinstance(json['actions'], list):
            for i, action in enumerate(json['actions']):
                errors.extend(__validate_action(i, action, config.actions))
        else:
            errors.append('actions: must be a list')

    # A night mismatch is the only non-fatal warning, so handle it here
    # and insert the warning message at the start of the list
    is_valid = len(errors) == 0

    if current_night != schedule_night:
        schedule_str = schedule_night.strftime('%Y-%m-%d')
        current_str = current_night.strftime('%Y-%m-%d')
        if require_tonight:
            is_valid = False
            errors.insert(0, f'night: {schedule_str} is not tonight ({current_str})')
        else:
            errors.insert(0, f'info: night {schedule_str} is not tonight ({current_str})')
    return is_valid, errors


def parse_schedule_actions(config, json):
    """
    Parses a json object into a list of TelescopeActions
    to be run by the telescope control thread
    """
    actions = []
    try:
        for action in json.get('actions', []):
            actions.append(config.actions[action['type']](config.log_name, action))
    except Exception:
        print('exception while parsing schedule')
        traceback.print_exc(file=sys.stdout)
        return []
    return actions


def parse_dome_window(json, config):
    """
    Parses dome open and close dates from a schedule
    Returns a tuple of the open and close dates
    or None if the json does not define a dome block
    """
    if 'dome' in json and 'open' in json['dome'] and 'close' in json['dome']:
        open_date = close_date = None
        if json['dome']['open'] == 'auto' or json['dome']['close'] == 'auto':
            open_date, close_date = night_start_end(json['night'], config)

        # These dates have already been validated by __validate_dome
        if json['dome']['open'] != 'auto':
            open_date = Time.strptime(json['dome']['open'], '%Y-%m-%dT%H:%M:%SZ')

        if json['dome']['close'] != 'auto':
            close_date = Time.strptime(json['dome']['close'], '%Y-%m-%dT%H:%M:%SZ')

        return open_date, close_date

    return None
