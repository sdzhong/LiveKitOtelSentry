/**
 * DISTRIBUTED TRACING FLOW:
 * 1. App generates a unique session_id on startup
 * 2. Sentry SDK auto-instruments app startup (ui.load) and HTTP requests
 * 3. tracePropagationTargets decides which URLs receive trace headers
 * 4. propagateTraceparent adds a W3C `traceparent` alongside `sentry-trace`, so the
 *    OTel-instrumented agent can continue the trace natively
 * 5. session_id tag correlates all traces (ui.load, lk.connect, agent) in Sentry
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
const release =
  process.env.SENTRY_RELEASE ||
  `livekit-voice-agent@${pkg.version || '0.0.0'}+${Platform.OS}`;

// Session ID links all traces (ui.load, lk.connect, agent) for correlation in Sentry
export const sessionId = `session-${Date.now()}-${Math.random()
  .toString(36)
  .slice(2, 11)}`;

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
  // URLs that should receive trace headers for distributed tracing
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
    // Send a W3C `traceparent` in addition to `sentry-trace` / `baggage`. Applies to
    // the same requests as tracePropagationTargets. Default is false.
    propagateTraceparent: true,
    // Structured logs via Sentry.logger.* (Sentry Logs).
    enableLogs: true,
  });

  Sentry.setTag('session_id', sessionId);
} else {
  console.warn('Sentry DSN missing; telemetry disabled');
}

registerGlobals();
// Sentry.wrap gives us app-start instrumentation (TTID/TTFD) and an error boundary.
AppRegistry.registerComponent(appName, () => Sentry.wrap(App));
