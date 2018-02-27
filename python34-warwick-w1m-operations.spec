#
# spec file for package python3-warwick-w1m-operations
#
# Copyright (c) 2016 SUSE LINUX Products GmbH, Nuernberg, Germany.
#
# All modifications and additions to the file contributed by third parties
# remain the property of their copyright owners, unless otherwise agreed
# upon. The license for this file, and modifications and additions to the
# file, is the same license as for the pristine package itself (unless the
# license for the pristine package is not an Open Source License, in which
# case the license is the MIT License). An "Open Source License" is a
# license that conforms to the Open Source Definition (Version 1.9)
# published by the Open Source Initiative.

Name:           python34-warwick-w1m-operations
Version:        2.0.2
Release:        0
License:        GPL3
Summary:        Common backend code for the Warwick one-metre telescope operations daemon
Url:            https://github.com/warwick-one-metre/
BuildArch:      noarch
Requires:       python34-astroplan, python34-jsonschema

%description
Part of the observatory software for the Warwick one-meter telescope.

python3-warwick-w1m-operations holds the common operations code.

%prep

rsync -av --exclude=build .. .

%build
python3 setup.py build

%install
python3 setup.py install --prefix=%{_prefix} --root=%{buildroot}

%files
%defattr(-,root,root,-)
%{python3_sitelib}/*

%changelog
