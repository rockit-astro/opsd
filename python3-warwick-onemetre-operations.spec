Name:           python3-warwick-onemetre-operations
Version:        20210916
Release:        0
License:        GPL3
Summary:        W1m specific operations code
Url:            https://github.com/warwick-one-metre/opsd
BuildArch:      noarch
Requires:       python3-warwick-observatory-operations, python3-warwick-observatory-dome, python3-astropy
Requires:       python3-warwick-observatory-talon, python3-warwick-observatory-pipeline, python3-warwick-observatory-andor-camera
# Currently broken and installed manually through pip
# Requires: python3-scipy

%description

%prep

rsync -av --exclude=build .. .

%build
%{__python3} setup_onemetre.py build

%install
%{__python3} setup_onemetre.py install --prefix=%{_prefix} --root=%{buildroot}
mkdir -p %{buildroot}%{_sysconfdir}/opsd
%{__install} %{_sourcedir}/onemetre.json %{buildroot}%{_sysconfdir}/opsd

%files
%defattr(-,root,root,-)
%{python3_sitelib}/*
%defattr(0644,root,root,-)
%{_sysconfdir}/opsd/onemetre.json

%changelog
