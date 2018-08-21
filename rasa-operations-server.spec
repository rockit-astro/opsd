Name:      rasa-operations-server
Version:   2.2.0
Release:   0
Url:       https://github.com/warwick-one-metre/opsd
Summary:   Operations server for the Warwick one-metre telescope.
License:   GPL-3.0
Group:     Unspecified
BuildArch: noarch
Requires:  python34, python34-numpy, python34-strict-rfc3339, python34-jsonschema, python34-Pyro4, python34-warwick-observatory-common, python34-warwick-w1m-operations, python34-warwick-w1m-pipeline, python34-warwick-w1m-environment, python34-warwick-w1m-dome, python34-warwick-w1m-camera, observatory-log-client, %{?systemd_requires}

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
