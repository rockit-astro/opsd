## Operations daemon

`opsd` is the top-level controller for robotic observatory operation.

`ops` is a commandline utility that controls the operations daemon.

### Configuration

Configuration is read from a json file that is installed by default to `/etc/opsd`.
A configuration file is specified when launching the server, and the `ops` frontend will search this location when launched.

```python
{
  "daemon": "superwasp_operations", # Run the server as this daemon. Daemon types are registered in `rockit.common.daemons`.
  "log_name": "opsd@superwasp", # The name to use when writing messages to the observatory log.
  "control_machines": ["SWASPTCS"], # Machine names that are allowed to control (rather than just query) state. Machine names are registered in `rockit.common.IP`.
  "pipeline_machines": ["SWASPTCS"], # Machine names that are allowed to notify pipelined frame metadata.
  "actions_module": "rockit.operations.actions.superwasp", # Python module to search for actions for this telescope.
  "loop_delay": 10, # Delay between loop updates.
  "site_latitude": "28.76022 N", # Telescope latitude used for scheduling calculations.
  "site_longitude": "17.87928 W", # Telescope longitude used for scheduling calculations.
  "site_elevation": 2350, # Telescope elevation used for scheduling calculations.
  "dome": {
    "module": "rockit.operations.dome.simulated", # Python module defining the dome interface logic.
    "open_delay": 60, # Module-specific configuration.
    "close_delay": 120
  },
  "environment_daemon": "observatory_environment", # Daemon to query environment state from. Daemon types are registered in `rockit.common.daemons`.
  "environment_conditions": [
    { # Each condition type can contain multiple sensors, pulled from the environment data dictionary.
      "label": "Wind", # Human readable label for this condition type (visible in ops output and web dashboard).
      "sensors": [ # A condition is considered unsafe if ANY sensor returns unsafe or if ALL sensors are unavailable.
        {
          "label": "W1m", # Human readable label for this condition sensor.
          "sensor": "w1m_vaisala", # Sensor name in environmentd data dictionary.
          "parameter": "wind_speed" # Sensor parameter name in environmentd data dictionary.
        }
        # Additional sensors can be defined
      ]
    }
    # Additional conditions can be defined
  }
}
```

Individual nightly plans are scheduled using `ops schedule plan.json` where `plan.json` takes the format
```python
{
  "night": "2021-07-13", # Start of night date for this plan.
  "dome": { # Times to open and close the dome. Can be omitted if opsd is not to control the dome.
    "open": "2021-07-13T21:00:00Z",
    "close": "2021-07-13T21:10:00Z"
  },
  "actions": [
    { # List of actions that are executed in order.
      "type": "Wait", # Investigate the actions python module referenced in the telescope config for a list of actions and their parameters.
      "delay": 120
    }
  ]
}
```

The dome and telescope should be in automatic mode before trying to schedule a plan.


### Initial Installation

The automated packaging scripts will push a collection of RPM packages to the observatory package repository:

| Package                               | Description                                                                      |
|---------------------------------------|----------------------------------------------------------------------------------|
| rockit-operations-server              | Contains the `opsd` server and systemd service file.                             |
| rockit-operations-client              | Contains the `ops` commandline utility for controlling the operations server.    |
| rockit-operations-data-<telescope>    | Contains the telescope-specific configuration.                                   |
| python3-rockit-operations             | Contains the python module with shared code for all telescopes.                  |
| python3-rockit-operations-<telescope> | Contains the python module with telescope-specific code (actions, dome control). |

After installing packages, the systemd service should be enabled:

```
sudo systemctl enable --now opsd@<config>
```

where `config` is the name of the json file for the appropriate telescope.

Now open a port in the firewall:
```
sudo firewall-cmd --zone=public --add-port=<port>/tcp --permanent
sudo firewall-cmd --reload
```
where `port` is the port defined in `rockit.common.daemons` for the daemon specified in the ops config.

### Upgrading Installation

New RPM packages are automatically created and pushed to the package repository for each push to the `master` branch.
These can be upgraded locally using the standard system update procedure:
```
sudo yum clean expire-cache
sudo yum update
```

The daemon should then be restarted to use the newly installed code:
```
sudo systemctl restart opsd@<config>
```

### Testing Locally

The ops server and client can be run directly from a git clone:
```
./opsd superwasp.json
OPSD_CONFIG_PATH=superwasp.json ./ops status
```
