Name:      observatory-operations-server
Version:   20210912
Release:   0
Url:       https://github.com/warwick-one-metre/opsd
Summary:   Operations server.
License:   GPL-3.0
Group:     Unspecified
BuildArch: noarch
Requires:  python3, python3-numpy, python3-strict-rfc3339, python3-jsonschema, python3-Pyro4,
Requires:  python3-warwick-observatory-common, python3-warwick-observatory-operations, %{?systemd_requires}

%description

%build
mkdir -p %{buildroot}%{_bindir}
mkdir -p %{buildroot}%{_unitdir}

%{__install} %{_sourcedir}/opsd %{buildroot}%{_bindir}
%{__install} %{_sourcedir}/opsd@.service %{buildroot}%{_unitdir}

%files
%defattr(0755,root,root,-)
%{_bindir}/opsd
%defattr(-,root,root,-)
%{_unitdir}/opsd@.service

%changelog
