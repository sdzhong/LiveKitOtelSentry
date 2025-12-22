/**
 * DISTRIBUTED TRACING FLOW:
 * 1. App generates a unique session_id on startup
 * 2. Sentry SDK auto-instruments app startup (ui.load) and HTTP requests
 * 3. tracePropagationTargets ensures sentry-trace headers are sent to backend
 * 4. session_id tag correlates all traces (ui.load, lk.connect, agent) in Sentry
 */

import {registerGlobals} from '@livekit/react-native';
import {AppRegistry, Platform} from 'react-native';
import * as Sentry from '@sentry/react-native';
import pkg from './package.json';
import App from './App';
import {name as appName} from './app.json';

const dsn = process.env.SENTRY_DSN || '';
const backendUrl = process.env.BACKEND_URL || 'http://127.0.0.1:8000';
const environment = process.env.SENTRY_ENV || 'dev';
const release = `livekit-voice-agent@${pkg.version || '0.0.0'}+${Platform.OS}`;

// Session ID links all traces (ui.load, lk.connect, agent) for correlation in Sentry
export const sessionId = `session-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;

const escapeForRegex = str => str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

const backendHostPattern = (() => {
  try {
    const url = new URL(backendUrl);
    return new RegExp(`^${escapeForRegex(url.origin)}`);
  } catch {
    return backendUrl;
  }
})();

if (dsn) {
  // URLs that should receive sentry-trace headers for distributed tracing
  const tracePropagationTargets = [
    backendUrl,
    backendHostPattern,
    /https?:\/\/10\.0\.2\.2(?::\d+)?/,
    /https?:\/\/127\.0\.0\.1(?::\d+)?/,
  ];

  Sentry.init({
    dsn,
    enableAutoPerformanceTracing: true,
    tracesSampleRate: 1.0,
    environment,
    release,
    tracePropagationTargets,
  });

  Sentry.setTag('session_id', sessionId);
} else {
  console.warn('Sentry DSN missing; telemetry disabled');
}

registerGlobals();
AppRegistry.registerComponent(appName, () => App);
