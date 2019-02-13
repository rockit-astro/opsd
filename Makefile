RPMBUILD = rpmbuild --define "_topdir %(pwd)/build" \
        --define "_builddir %{_topdir}" \
        --define "_rpmdir %{_topdir}" \
        --define "_srcrpmdir %{_topdir}" \
        --define "_sourcedir %(pwd)"

all:
	mkdir -p build
	${RPMBUILD} -ba onemetre-operations-server.spec
	mv ops ops.bak
	sed 's/RASAConfig/OneMetreConfig/' ops.bak > ops
	${RPMBUILD} -ba onemetre-operations-client.spec
	mv ops.bak ops
	${RPMBUILD} -ba rasa-operations-server.spec
	${RPMBUILD} -ba rasa-operations-client.spec
	${RPMBUILD} -ba python36-warwick-observatory-operations.spec
	${RPMBUILD} -ba python36-warwick-w1m-operations.spec
	${RPMBUILD} -ba python36-warwick-rasa-operations.spec
	mv build/noarch/*.rpm .
	rm -rf build

