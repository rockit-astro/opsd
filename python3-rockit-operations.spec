%if %{undefined _telescope}
Name:           python3-rockit-operations
Summary:        Common backend code for the operations daemon
%else
Name:           python3-rockit-operations-%{_telescope}
Summary:        %{_label} specific operations code
%endif
Version:        %{_version}
Release:        1%{dist}
License:        GPL3
Url:            https://github.com/rockit-astro/opsd
BuildArch:      noarch
BuildRequires:  python3-devel

%description

%prep
rsync -av --exclude=build --exclude=.git --exclude=.github .. .

%if %{defined _telescope}
mv setup.cfg.%{_telescope} setup.cfg
%endif


%generate_buildrequires
%pyproject_buildrequires -R

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files rockit

%files -f %{pyproject_files}
