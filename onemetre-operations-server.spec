Name:      onemetre-operations-server
Version:   2.3.2
Release:   0
Url:       https://github.com/warwick-one-metre/opsd
Summary:   Operations server for the Warwick one-metre telescope.
License:   GPL-3.0
Group:     Unspecified
BuildArch: noarch
Requires:  python3, python3-numpy, python3-strict-rfc3339, python3-jsonschema, python3-Pyro4, python3-pyephem
Requires:  python3-warwick-observatory-common, python3-warwick-w1m-operations, python3-warwick-w1m-pipeline
Requires:  python3-warwick-observatory-environment, python3-warwick-observatory-dome, python3-warwick-w1m-camera
Requires:  observatory-log-client, %{?systemd_requires}

%description
Part of the observatory software for the Warwick one-meter telescope.

opsd is the daemon that controls the top-level automatic observatory control.

%build
mkdir -p %{buildroot}%{_bindir}
mkdir -p %{buildroot}%{_unitdir}

%{__install} %{_sourcedir}/opsd %{buildroot}%{_bindir}
%{__install} %{_sourcedir}/opsd.service %{buildroot}%{_unitdir}

%post
%systemd_post opsd.service

%preun
%systemd_preun opsd.service

%postun
%systemd_postun_with_restart opsd.service

%files
%defattr(0755,root,root,-)
%{_bindir}/opsd
%defattr(-,root,root,-)
%{_unitdir}/opsd.service

%changelog
