Name:      onemetre-operations-server
Version:   2.0.1
Release:   0
Url:       https://github.com/warwick-one-metre/opsd
Summary:   Operations server for the Warwick one-metre telescope.
License:   GPL-3.0
Group:     Unspecified
BuildArch: noarch
%if 0%{?suse_version}
Requires:  python3, python34-Pyro4, python34-warwick-observatory-common, python34-warwick-w1m-operations, python34-warwick-w1m-environment, observatory-log-client, %{?systemd_requires}
BuildRequires: systemd-rpm-macros
%endif
%if 0%{?centos_ver}
Requires:  python34, python34-Pyro4, python34-warwick-observatory-common, python34-warwick-w1m-operations, python34-warwick-w1m-environment, observatory-log-client, %{?systemd_requires}
%endif

%description
Part of the observatory software for the Warwick one-meter telescope.

opsd is the daemon that controls the top-level automatic observatory control.

%build
mkdir -p %{buildroot}%{_bindir}
mkdir -p %{buildroot}%{_unitdir}

%{__install} %{_sourcedir}/opsd %{buildroot}%{_bindir}
%{__install} %{_sourcedir}/opsd.service %{buildroot}%{_unitdir}

%pre
%if 0%{?suse_version}
%service_add_pre opsd.service
%endif

%post
%if 0%{?suse_version}
%service_add_post opsd.service
%endif
%if 0%{?centos_ver}
%systemd_post opsd.service
%endif

%preun
%if 0%{?suse_version}
%stop_on_removal opsd.service
%service_del_preun opsd.service
%endif
%if 0%{?centos_ver}
%systemd_preun opsd.service
%endif

%postun
%if 0%{?suse_version}
%restart_on_update opsd.service
%service_del_postun opsd.service
%endif
%if 0%{?centos_ver}
%systemd_postun_with_restart opsd.service
%endif

%files
%defattr(0755,root,root,-)
%{_bindir}/opsd
%defattr(-,root,root,-)
%{_unitdir}/opsd.service

%changelog
