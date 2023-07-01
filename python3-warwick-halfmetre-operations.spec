Name:           python3-warwick-halfmetre-operations
Version:        20230701
Release:        0
License:        GPL3
Summary:        CLASP specific operations code
Url:            https://github.com/warwick-one-metre/opsd
BuildArch:      noarch
Requires:       python3-warwick-observatory-operations python3-astropy python3-warwick-observatory-lmount python3-scipy
Requires:       python3-warwick-observatory-efafocus python3-warwick-observatory-qhy-camera python3-warwick-observatory-pipeline

%description

%prep

rsync -av --exclude=build .. .

%build
%{__python3} setup_halfmetre.py build

%install
%{__python3} setup_clasp.py install --prefix=%{_prefix} --root=%{buildroot}
mkdir -p %{buildroot}%{_sysconfdir}/opsd
%{__install} %{_sourcedir}/halfmetre.json %{buildroot}%{_sysconfdir}/opsd

%files
%defattr(-,root,root,-)
%{python3_sitelib}/*
%defattr(0644,root,root,-)
%{_sysconfdir}/opsd/halfmetre.json

%changelog
