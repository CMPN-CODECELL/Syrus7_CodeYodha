-- Separate databases keep the paper broker's state independent of the app, like a real external broker.
CREATE DATABASE paperbroker OWNER countersign;
CREATE DATABASE countersign_dbos OWNER countersign;
CREATE DATABASE countersign_test OWNER countersign;
