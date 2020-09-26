Name:      rasa-operations-server
Version:   2.3.2
Release:   0
Url:       https://github.com/warwick-one-metre/opsd
Summary:   Operations server for the RASA prototype telescope.
License:   GPL-3.0
Group:     Unspecified
BuildArch: noarch
Requires:  python3, python3-numpy, python3-strict-rfc3339, python3-jsonschema, python3-Pyro4
Requires:  python3-warwick-observatory-common, python3-warwick-rasa-operations, python3-warwick-rasa-pipeline
Requires:  python3-warwick-observatory-environment, python3-warwick-observatory-dome, python3-warwick-rasa-camera
Requires:  observatory-log-client, %{?systemd_requires}

%description
Part of the observatory software for the RASA prototype telescope.

opsd is the daemon that controls the top-level automatic observatory control.

%build
mkdir -p %{buildroot}%{_bindir}
mkdir -p %{buildroot}%{_unitdir}

%{__install} %{_sourcedir}/opsd %{buildroot}%{_bindir}
%{__install} %{_sourcedir}/rasa-opsd.service %{buildroot}%{_unitdir}

%post
%systemd_post rasa-opsd.service

%preun
%systemd_preun rasa-opsd.service

%postun
%systemd_postun_with_restart rasa-opsd.service

%files
%defattr(0755,root,root,-)
%{_bindir}/opsd
%defattr(-,root,root,-)
%{_unitdir}/rasa-opsd.service

%changelog
