RPMBUILD = rpmbuild --define "_topdir %(pwd)/build" \
        --define "_builddir %{_topdir}" \
        --define "_rpmdir %{_topdir}" \
        --define "_srcrpmdir %{_topdir}" \
        --define "_sourcedir %(pwd)"

all:
	mkdir -p build
	${RPMBUILD} -ba rasa-operations-server.spec
	${RPMBUILD} -ba rasa-operations-client.spec
	${RPMBUILD} -ba python34-warwick-observatory-operations.spec
	${RPMBUILD} -ba python34-warwick-rasa-operations.spec
	mv build/noarch/*.rpm .
	rm -rf build

