# LiveKitVoiceAgent (mobile)

The React Native client for the distributed-tracing demo. **Setup, environment variables
and the full run flow live in the [root README](../README.md)** — start there.

This app is the "head" of the trace: it generates a `session_id`, starts a `lk.connect`
trace, and propagates it to the token server with both `sentry-trace` and (via
`propagateTraceparent`) a W3C `traceparent` header.

## Quick reference

```sh
npm install
cd ios && pod install && cd ..

cp .env.example .env      # then fill in BACKEND_URL, LIVEKIT_URL, SENTRY_DSN

npx react-native start --reset-cache   # terminal 1
npm run ios                            # terminal 2
```

`.env` is read at bundle time by `babel-plugin-inline-dotenv` (see `babel.config.js`), so
**restart Metro with `--reset-cache` after changing it**.

## Where the Sentry wiring lives

| File | Responsibility |
|---|---|
| `index.js` | `Sentry.init`, `session_id` generation, `Sentry.wrap(App)` |
| `src/VoiceAgent.tsx` | `lk.connect` trace, token fetch, LiveKit room, `/fail` test button |

## Requirements

iOS 15+ and Xcode 16.4+ (required by `@sentry/react-native` 8.x). Run `pod install` after
any dependency bump.
