Name:           python36-warwick-w1m-operations
Version:        2.3.1
Release:        0
License:        GPL3
Summary:        RASA prototype specific operations code
Url:            https://github.com/warwick-one-metre/
BuildArch:      noarch
Requires:       python36-warwick-observatory-operations

%description
Part of the observatory software for the Warwick one-meter telescope.

python36-warwick-w1m-operations holds the W1m-specific operations code.

%prep

rsync -av --exclude=build .. .

%build
%{__python3} setup_w1m.py build

%install
%{__python3} setup_w1m.py install --prefix=%{_prefix} --root=%{buildroot}

%files
%defattr(-,root,root,-)
%{python3_sitelib}/*

%changelog
