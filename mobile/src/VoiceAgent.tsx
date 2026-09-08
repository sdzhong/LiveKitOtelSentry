/**
 * DISTRIBUTED TRACING FLOW:
 * 1. connect() creates a new trace (separate from ui.load) for the LiveKit session
 * 2. getToken() fetches token with x-session-id header for correlation; the Sentry SDK
 *    attaches sentry-trace, baggage and (via propagateTraceparent) W3C traceparent
 * 3. Token server injects trace context into token metadata
 * 4. Agent extracts metadata and continues the trace
 * 5. callBackendError() tests distributed tracing with the /fail endpoint
 *
 * All endpoints come from mobile/.env (see .env.example) -- nothing is hardcoded.
 */

import React, {useState, useCallback, useEffect, useRef} from 'react';
import {
  View,
  Text,
  TouchableOpacity,
  StyleSheet,
  Animated,
  Easing,
  Platform,
} from 'react-native';
import * as Sentry from '@sentry/react-native';
import {
  Room,
  RoomEvent,
  ConnectionState,
  Participant,
  Track,
  RemoteTrack,
  RemoteTrackPublication,
} from 'livekit-client';
import {sessionId} from '../index';

declare const process: {env: Record<string, string | undefined>};

const LIVEKIT_URL = process.env.LIVEKIT_URL || '';
const ROOM_NAME = process.env.LK_ROOM || 'voice-room';
const BACKEND_URL =
  process.env.BACKEND_URL ||
  (Platform.OS === 'android' ? 'http://10.0.2.2:8000' : 'http://127.0.0.1:8000');

const getToken = async (identity: string): Promise<string> => {
  const response = await fetch(`${BACKEND_URL}/token`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'x-session-id': sessionId,
    },
    body: JSON.stringify({identity, room: ROOM_NAME, name: identity}),
  });

  if (!response.ok) {
    const body = await response.text();
    throw new Error(`/token responded ${response.status}: ${body}`);
  }

  const data = await response.json();
  if (!data?.token) {
    throw new Error('Token server response missing token');
  }
  return data.token;
};

const VoiceAgent: React.FC = () => {
  const [room] = useState(() => new Room());
  const requestingTokenRef = useRef(false);
  const [identity] = useState(() => `mobile-${Date.now()}`);
  const [connectionState, setConnectionState] = useState<ConnectionState>(
    ConnectionState.Disconnected,
  );
  const [isAgentSpeaking, setIsAgentSpeaking] = useState(false);
  const [isUserSpeaking, setIsUserSpeaking] = useState(false);
  const [statusMessage, setStatusMessage] = useState('Tap to connect');
  const [pulseAnim] = useState(new Animated.Value(1));

  useEffect(() => {
    if (isAgentSpeaking || isUserSpeaking) {
      Animated.loop(
        Animated.sequence([
          Animated.timing(pulseAnim, {
            toValue: 1.3,
            duration: 400,
            easing: Easing.inOut(Easing.ease),
            useNativeDriver: true,
          }),
          Animated.timing(pulseAnim, {
            toValue: 1,
            duration: 400,
            easing: Easing.inOut(Easing.ease),
            useNativeDriver: true,
          }),
        ]),
      ).start();
    } else {
      pulseAnim.setValue(1);
    }
  }, [isAgentSpeaking, isUserSpeaking, pulseAnim]);

  useEffect(() => {
    const handleConnectionStateChange = (state: ConnectionState) => {
      setConnectionState(state);
      switch (state) {
        case ConnectionState.Connecting:
          setStatusMessage('Connecting...');
          break;
        case ConnectionState.Connected:
          setStatusMessage('Connected - Speak to the agent');
          break;
        case ConnectionState.Disconnected:
          setStatusMessage('Tap to connect');
          setIsAgentSpeaking(false);
          setIsUserSpeaking(false);
          break;
        case ConnectionState.Reconnecting:
          setStatusMessage('Reconnecting...');
          break;
      }
    };

    const handleTrackSubscribed = (
      track: RemoteTrack,
      _publication: RemoteTrackPublication,
      _participant: Participant,
    ) => {
      if (track.kind === Track.Kind.Audio) {
        console.log('Agent audio track subscribed');
      }
    };

    const handleActiveSpeakersChanged = (speakers: Participant[]) => {
      const agentSpeaking = speakers.some(
        s => s.identity?.startsWith('agent') || (s as any).isAgent,
      );
      const userSpeaking = speakers.some(
        s => !s.identity?.startsWith('agent') && !(s as any).isAgent,
      );
      setIsAgentSpeaking(agentSpeaking);
      setIsUserSpeaking(userSpeaking);
    };

    room.on(RoomEvent.ConnectionStateChanged, handleConnectionStateChange);
    room.on(RoomEvent.TrackSubscribed, handleTrackSubscribed);
    room.on(RoomEvent.ActiveSpeakersChanged, handleActiveSpeakersChanged);

    return () => {
      room.off(RoomEvent.ConnectionStateChanged, handleConnectionStateChange);
      room.off(RoomEvent.TrackSubscribed, handleTrackSubscribed);
      room.off(RoomEvent.ActiveSpeakersChanged, handleActiveSpeakersChanged);
    };
  }, [room]);

  // Creates a NEW trace (separate from ui.load) for accurate session duration
  const connect = useCallback(() => {
    if (requestingTokenRef.current) return;

    Sentry.withScope(scope => {
      const newTraceId = Array.from({length: 32}, () =>
        Math.floor(Math.random() * 16).toString(16),
      ).join('');
      scope.setPropagationContext({traceId: newTraceId, sampleRand: Math.random()});

      Sentry.startSpan(
        {name: 'lk.connect', op: 'lk.session', forceTransaction: true},
        async () => {
          try {
            if (!LIVEKIT_URL) {
              throw new Error(
                'LIVEKIT_URL is not set. Add it to mobile/.env and restart Metro with --reset-cache.',
              );
            }

            requestingTokenRef.current = true;
            setStatusMessage('Getting token...');
            const token = await getToken(identity);

            setStatusMessage('Connecting...');
            await room.connect(LIVEKIT_URL, token, {autoSubscribe: true});
            await room.localParticipant.setMicrophoneEnabled(true);
            Sentry.logger.info('Connected to LiveKit room', {
              room: ROOM_NAME,
              identity,
              session_id: sessionId,
            });
            setStatusMessage('Connected - Speak to the agent');
          } catch (error) {
            console.error('Connection error:', error);
            setStatusMessage(
              error instanceof Error ? error.message : 'Connection failed',
            );
            throw error;
          } finally {
            requestingTokenRef.current = false;
          }
        },
      );
    });
  }, [room, identity]);

  const disconnect = useCallback(async () => {
    await room.disconnect();
    Sentry.logger.info('Disconnected from LiveKit room', {session_id: sessionId});
    setStatusMessage('Disconnected');
  }, [room]);

  const handlePress = useCallback(() => {
    if (connectionState === ConnectionState.Connected) {
      disconnect();
    } else if (connectionState === ConnectionState.Disconnected) {
      connect();
    }
  }, [connectionState, connect, disconnect]);

  // Tests distributed tracing: frontend span → backend span (correlated by session_id)
  const callBackendError = useCallback(() => {
    setStatusMessage('Calling backend /fail ...');
    Sentry.startSpan({name: 'debug.call_backend_fail', op: 'http.client'}, async () => {
      const resp = await fetch(`${BACKEND_URL}/fail`, {
        headers: {'x-session-id': sessionId},
      });
      if (!resp.ok) {
        const body = await resp.text();
        throw new Error(`/fail responded ${resp.status}: ${body}`);
      }
    }).catch(() => {
      setStatusMessage('Backend error captured (see Sentry)');
    });
  }, []);

  const isConnected = connectionState === ConnectionState.Connected;
  const isConnecting = connectionState === ConnectionState.Connecting;

  return (
    <View style={styles.container}>
      <Text style={styles.title}>Voice Agent</Text>
      <Text style={styles.subtitle}>LiveKit + OpenAI</Text>

      <View style={styles.indicatorContainer}>
        <Animated.View
          style={[
            styles.outerRing,
            isAgentSpeaking && styles.outerRingActive,
            {transform: [{scale: pulseAnim}]},
          ]}
        />
        <TouchableOpacity
          style={[
            styles.mainButton,
            isConnected && styles.mainButtonConnected,
            isConnecting && styles.mainButtonConnecting,
          ]}
          onPress={handlePress}
          activeOpacity={0.8}>
          <View style={styles.innerCircle}>
            {isConnected ? (
              <View style={styles.micIcon}>
                <View style={styles.micBody} />
                <View style={styles.micBase} />
              </View>
            ) : (
              <Text style={styles.connectIcon}>🎤</Text>
            )}
          </View>
        </TouchableOpacity>
      </View>

      <View style={styles.statusContainer}>
        <Text style={styles.statusText}>{statusMessage}</Text>
        {isConnected && (
          <View style={styles.speakingIndicators}>
            <View style={styles.indicatorRow}>
              <View
                style={[
                  styles.dot,
                  isAgentSpeaking ? styles.dotActive : styles.dotInactive,
                ]}
              />
              <Text style={styles.indicatorText}>Agent</Text>
            </View>
            <View style={styles.indicatorRow}>
              <View
                style={[
                  styles.dot,
                  isUserSpeaking ? styles.dotActiveUser : styles.dotInactive,
                ]}
              />
              <Text style={styles.indicatorText}>You</Text>
            </View>
          </View>
        )}
      </View>

      {isConnected && (
        <Text style={styles.hintText}>Tap the circle to disconnect</Text>
      )}

      <View style={styles.debugContainer}>
        <TouchableOpacity style={styles.debugButton} onPress={callBackendError}>
          <Text style={styles.debugButtonText}>Call Backend (error)</Text>
        </TouchableOpacity>
      </View>
    </View>
  );
};

const styles = StyleSheet.create({
  container: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: '#0a0a0f',
    padding: 20,
  },
  title: {
    fontSize: 32,
    fontWeight: '700',
    color: '#ffffff',
    marginBottom: 4,
    letterSpacing: 1,
  },
  subtitle: {
    fontSize: 14,
    color: '#6b7280',
    marginBottom: 60,
    letterSpacing: 2,
    textTransform: 'uppercase',
  },
  indicatorContainer: {
    alignItems: 'center',
    justifyContent: 'center',
    marginBottom: 50,
  },
  outerRing: {
    position: 'absolute',
    width: 200,
    height: 200,
    borderRadius: 100,
    borderWidth: 2,
    borderColor: 'rgba(99, 102, 241, 0.2)',
  },
  outerRingActive: {
    borderColor: 'rgba(99, 102, 241, 0.6)',
    backgroundColor: 'rgba(99, 102, 241, 0.1)',
  },
  mainButton: {
    width: 160,
    height: 160,
    borderRadius: 80,
    backgroundColor: '#1a1a2e',
    alignItems: 'center',
    justifyContent: 'center',
    borderWidth: 3,
    borderColor: '#2d2d44',
    shadowColor: '#6366f1',
    shadowOffset: {width: 0, height: 0},
    shadowOpacity: 0.3,
    shadowRadius: 20,
    elevation: 10,
  },
  mainButtonConnected: {
    backgroundColor: '#1e1e3f',
    borderColor: '#6366f1',
  },
  mainButtonConnecting: {
    backgroundColor: '#1a1a2e',
    borderColor: '#4b5563',
  },
  innerCircle: {
    width: 80,
    height: 80,
    borderRadius: 40,
    backgroundColor: '#0a0a0f',
    alignItems: 'center',
    justifyContent: 'center',
  },
  micIcon: {
    alignItems: 'center',
  },
  micBody: {
    width: 20,
    height: 30,
    backgroundColor: '#6366f1',
    borderRadius: 10,
  },
  micBase: {
    width: 30,
    height: 4,
    backgroundColor: '#6366f1',
    borderRadius: 2,
    marginTop: 4,
  },
  connectIcon: {
    fontSize: 36,
  },
  statusContainer: {
    alignItems: 'center',
    minHeight: 80,
  },
  statusText: {
    fontSize: 16,
    color: '#9ca3af',
    marginBottom: 20,
    textAlign: 'center',
  },
  speakingIndicators: {
    flexDirection: 'row',
    gap: 30,
  },
  indicatorRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 8,
  },
  dot: {
    width: 12,
    height: 12,
    borderRadius: 6,
  },
  dotActive: {
    backgroundColor: '#6366f1',
    shadowColor: '#6366f1',
    shadowOffset: {width: 0, height: 0},
    shadowOpacity: 0.8,
    shadowRadius: 8,
  },
  dotActiveUser: {
    backgroundColor: '#10b981',
    shadowColor: '#10b981',
    shadowOffset: {width: 0, height: 0},
    shadowOpacity: 0.8,
    shadowRadius: 8,
  },
  dotInactive: {
    backgroundColor: '#374151',
  },
  indicatorText: {
    fontSize: 14,
    color: '#9ca3af',
  },
  hintText: {
    position: 'absolute',
    bottom: 50,
    fontSize: 12,
    color: '#4b5563',
  },
  debugContainer: {
    position: 'absolute',
    bottom: 80,
    width: '100%',
    paddingHorizontal: 20,
    gap: 12,
  },
  debugButton: {
    backgroundColor: '#111827',
    borderColor: '#374151',
    borderWidth: 1,
    borderRadius: 8,
    paddingVertical: 10,
    alignItems: 'center',
  },
  debugButtonText: {
    color: '#e5e7eb',
    fontSize: 14,
    fontWeight: '600',
  },
});

export default VoiceAgent;
