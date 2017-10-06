## W1m operations daemon [![Travis CI build status](https://travis-ci.org/warwick-one-metre/opsd.svg?branch=master)](https://travis-ci.org/warwick-one-metre/opsd)

Part of the observatory software for the Warwick one-meter telescope.

`opsd` is the top-level controller for robotic observatory operation.

`ops` is a commandline utility that controls the operations daemon.

`python34-warwick-w1m-operations` is a python module with the common ops code.

See [Software Infrastructure](https://github.com/warwick-one-metre/docs/wiki/Software-Infrastructure) for an overview of the W1m software architecture and instructions for developing and deploying the code.

### Software Setup

After installing `onemetre-ops-server`, the `opsd` must be enabled using:
```
sudo systemctl enable opsd.service
```

The service will automatically start on system boot, or you can start it immediately using:
```
sudo systemctl start opsd.service
```
