const path = require('path');

// Force-load mobile/.env before Babel runs so these values override any
// exported shell variables (e.g., backend SENTRY_DSN).
require('dotenv').config({
  path: path.join(__dirname, '.env'),
  override: true,
});

module.exports = {
  presets: ['module:@react-native/babel-preset'],
  plugins: [
    [
      'inline-dotenv',
      {
        // Use an absolute path so Metro picks mobile/.env regardless of cwd.
        path: `${__dirname}/.env`,
        silent: true,
        override: true, // mobile values should win over shell env
      },
      'inline-dotenv-mobile',
    ],
  ],
};
