Name:      rasa-operations-client
Version:   2.3.2
Release:   0
Url:       https://github.com/warwick-one-metre/opsd
Summary:   Operations client for the RASA prototype telescope.
License:   GPL-3.0
Group:     Unspecified
BuildArch: noarch
Requires:  python3, python3-Pyro4, python3-warwick-observatory-common, python3-warwick-rasa-operations, python3-astropy

%description
Part of the observatory software for the RASA prototype telescope.

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
