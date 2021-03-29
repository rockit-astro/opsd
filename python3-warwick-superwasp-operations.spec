Name:           python3-warwick-superwasp-operations
Version:        20210713
Release:        0
License:        GPL3
Summary:        SuperWASP specific operations code
Url:            https://github.com/warwick-one-metre/opsd
BuildArch:      noarch
Requires:       python3-warwick-observatory-operations, python3-warwick-observatory-talon, python3-warwick-observatory-camera-atik

%description

%prep

rsync -av --exclude=build .. .

%build
%{__python3} setup_superwasp.py build

%install
%{__python3} setup_superwasp.py install --prefix=%{_prefix} --root=%{buildroot}
mkdir -p %{buildroot}%{_sysconfdir}/opsd
%{__install} %{_sourcedir}/superwasp.json %{buildroot}%{_sysconfdir}/opsd

%files
%defattr(-,root,root,-)
%{python3_sitelib}/*
%defattr(0644,root,root,-)
%{_sysconfdir}/opsd/superwasp.json

%changelog
