Name:      onemetre-ops-server
Version:   1.2
Release:   1
Url:       https://github.com/warwick-one-metre/opsd
Summary:   Operations server for the Warwick one-metre telescope.
License:   GPL-3.0
Group:     Unspecified
BuildArch: noarch
Requires:  python3, python3-Pyro4, python3-warwickobservatory, onemetre-obslog-client, %{?systemd_requires}
BuildRequires: systemd-rpm-macros

%description
Part of the observatory software for the Warwick one-meter telescope.

opsd is the daemon that controls the top-level automatic observatory control.

%build
mkdir -p %{buildroot}%{_bindir}
mkdir -p %{buildroot}%{_unitdir}

%{__install} %{_sourcedir}/opsd %{buildroot}%{_bindir}
%{__install} %{_sourcedir}/opsd.service %{buildroot}%{_unitdir}

%pre
%service_add_pre opd.service

%post
%service_add_post opsd.service

%preun
%stop_on_removal opsd.service
%service_del_preun opsd.service

%postun
%restart_on_update opsd.service
%service_del_postun opsd.service

%files
%defattr(0755,root,root,-)
%{_bindir}/opsd
%defattr(-,root,root,-)
%{_unitdir}/opsd.service

%changelog
