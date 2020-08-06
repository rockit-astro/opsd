RPMBUILD = rpmbuild --define "_topdir %(pwd)/build" \
        --define "_builddir %{_topdir}" \
        --define "_rpmdir %{_topdir}" \
        --define "_srcrpmdir %{_topdir}" \
        --define "_sourcedir %(pwd)"

all:
	mkdir -p build
	${RPMBUILD} -ba onemetre-operations-server.spec
	${RPMBUILD} -ba onemetre-operations-client.spec
	${RPMBUILD} -ba rasa-operations-server.spec
	mv ops ops.bak
	sed "s/TELESCOPE = 'onemetre'/TELESCOPE = 'rasa'/" ops.bak > ops
	${RPMBUILD} -ba rasa-operations-client.spec
	mv ops.bak ops
	rm -rf build/build
	${RPMBUILD} -ba python3-warwick-observatory-operations.spec
	rm -rf build/build
	${RPMBUILD} -ba python3-warwick-w1m-operations.spec
	rm -rf build/build
	${RPMBUILD} -ba python3-warwick-rasa-operations.spec
	mv build/noarch/*.rpm .
	rm -rf build

