RPMBUILD = rpmbuild --define "_topdir %(pwd)/build" \
        --define "_builddir %{_topdir}" \
        --define "_rpmdir %{_topdir}" \
        --define "_srcrpmdir %{_topdir}" \
        --define "_sourcedir %(pwd)"

all:
	mkdir -p build
	${RPMBUILD} -ba observatory-operations-server.spec
	${RPMBUILD} -ba observatory-operations-client.spec
	rm -rf build/build
	${RPMBUILD} -ba python3-warwick-observatory-operations.spec
	rm -rf build/build
	${RPMBUILD} -ba python3-warwick-onemetre-operations.spec
	rm -rf build/build
	${RPMBUILD} -ba python3-warwick-superwasp-operations.spec
	rm -rf build/build
	${RPMBUILD} -ba python3-warwick-clasp-operations.spec
	mv build/noarch/*.rpm .
	rm -rf build

