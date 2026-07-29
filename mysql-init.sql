-- MySQL initialization for AI Meeting Bot
-- Runs once on first container start (mysql:8.0 entrypoint)

-- Grant full privileges to appuser on the application database
GRANT ALL PRIVILEGES ON ai_meeting_db.* TO 'appuser'@'%';
FLUSH PRIVILEGES;
