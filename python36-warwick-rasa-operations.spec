Name:           python36-warwick-rasa-operations
Version:        2.3.0
Release:        0
License:        GPL3
Summary:        RASA prototype specific operations code
Url:            https://github.com/warwick-one-metre/
BuildArch:      noarch
Requires:       python36-warwick-rasa-operations

%description
Part of the observatory software for the RASA prototype telescope.

python36-warwick-rasa-operations holds the RASA-specific operations code.

%prep

rsync -av --exclude=build .. .

%build
%{__python3_other} setup_rasa.py build

%install
%{__python3_other} setup_rasa.py install --prefix=%{_prefix} --root=%{buildroot}

%files
%defattr(-,root,root,-)
%{python3_other_sitelib}/*

%changelog
