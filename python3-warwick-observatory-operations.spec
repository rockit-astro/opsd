Name:           python3-warwick-observatory-operations
Version:        20211119
Release:        0
License:        GPL3
Summary:        Common backend code for the operations daemon
Url:            https://github.com/warwick-one-metre/opsd
BuildArch:      noarch
Requires:       python3-astropy python3-jsonschema python3-skyfield

%description

%prep

rsync -av --exclude=build .. .

%build
%{__python3} setup_observatory.py build

%install
%{__python3} setup_observatory.py install --prefix=%{_prefix} --root=%{buildroot}

%files
%defattr(-,root,root,-)
%{python3_sitelib}/*

%changelog
