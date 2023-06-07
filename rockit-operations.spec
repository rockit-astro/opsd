Name:      rockit-operations
Version:   %{_version}
Release:   1
Summary:   Observatory automation code
Url:       https://github.com/rockit-astro/opsd
License:   GPL-3.0
BuildArch: noarch

%description


%build
mkdir -p %{buildroot}%{_bindir}
mkdir -p %{buildroot}%{_unitdir}
mkdir -p %{buildroot}/etc/bash_completion.d
mkdir -p %{buildroot}%{_sysconfdir}/opsd

%{__install} %{_sourcedir}/ops %{buildroot}%{_bindir}
%{__install} %{_sourcedir}/opsd %{buildroot}%{_bindir}
%{__install} %{_sourcedir}/opsd@.service %{buildroot}%{_unitdir}
%{__install} %{_sourcedir}/completion/ops %{buildroot}/etc/bash_completion.d

%{__install} %{_sourcedir}/config/clasp.json %{buildroot}%{_sysconfdir}/opsd
%{__install} %{_sourcedir}/config/halfmetre.json %{buildroot}%{_sysconfdir}/opsd
%{__install} %{_sourcedir}/config/onemetre.json %{buildroot}%{_sysconfdir}/opsd
%{__install} %{_sourcedir}/config/superwasp.json %{buildroot}%{_sysconfdir}/opsd
%{__install} %{_sourcedir}/config/warwick.json %{buildroot}%{_sysconfdir}/opsd

%package server
Summary:  Operations server
Group:    Unspecified
Requires: python3-rockit-operations
%description server

%files server
%defattr(0755,root,root,-)
%{_bindir}/opsd
%defattr(0644,root,root,-)
%{_unitdir}/opsd@.service

%package client
Summary:  Operations client
Group:    Unspecified
Requires: python3-rockit-operations
%description client

%files client
%defattr(0755,root,root,-)
%{_bindir}/ops
/etc/bash_completion.d/ops

%package data-clasp
Summary: Operations data for CLASP telescope
Group:   Unspecified
Requires: python3-rockit-operations-clasp
%description data-clasp

%files data-clasp
%defattr(0644,root,root,-)
%{_sysconfdir}/opsd/clasp.json

%package data-halfmetre
Summary: Operations data for the half metre telescope
Group:   Unspecified
Requires: python3-rockit-operations-halfmetre
%description data-halfmetre

%files data-halfmetre
%defattr(0644,root,root,-)
%{_sysconfdir}/opsd/halfmetre.json

%package data-onemetre
Summary: Operations data for W1m telescope
Group:   Unspecified
Requires: python3-rockit-operations-onemetre
%description data-onemetre

%files data-onemetre
%defattr(0644,root,root,-)
%{_sysconfdir}/opsd/onemetre.json

%package data-superwasp
Summary: Operations data for SuperWASP telescope
Group:   Unspecified
Requires: python3-rockit-operations-superwasp
%description data-superwasp

%files data-superwasp
%defattr(0644,root,root,-)
%{_sysconfdir}/opsd/superwasp.json

%package data-warwick
Summary: Operations data for Windmill Hill observatory
Group:   Unspecified
Requires: python3-rockit-operations-warwick
%description data-warwick

%files data-warwick
%defattr(0644,root,root,-)
%{_sysconfdir}/opsd/warwick.json

%changelog
