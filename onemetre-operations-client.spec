Name:      onemetre-operations-client
Version:   2.0.1
Release:   0
Url:       https://github.com/warwick-one-metre/opsd
Summary:   Operations client for the Warwick one-metre telescope.
License:   GPL-3.0
Group:     Unspecified
BuildArch: noarch
%if 0%{?suse_version}
Requires:  python3, python34-Pyro4, python34-warwick-observatory-common, python34-warwick-w1m-operations, python34-astropy
%endif
%if 0%{?centos_ver}
Requires:  python34, python34-Pyro4, python34-warwick-observatory-common, python34-warwick-w1m-operations, python34-astropy
%endif

%description
Part of the observatory software for the Warwick one-meter telescope.

ops is a commandline utility for configuring the operational status of the observatory.

%build
mkdir -p %{buildroot}%{_bindir}
mkdir -p %{buildroot}/etc/bash_completion.d
%{__install} %{_sourcedir}/ops %{buildroot}%{_bindir}
%{__install} %{_sourcedir}/completion/ops %{buildroot}/etc/bash_completion.d/ops

%files
%defattr(0755,root,root,-)
%{_bindir}/ops
/etc/bash_completion.d/ops

%changelog
