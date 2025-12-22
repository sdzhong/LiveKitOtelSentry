import React from 'react';
import {SafeAreaView, StatusBar, StyleSheet} from 'react-native';
import VoiceAgent from './src/VoiceAgent';

const App: React.FC = () => {
  return (
    <SafeAreaView style={styles.container}>
      <StatusBar barStyle="light-content" backgroundColor="#0a0a0f" />
      <VoiceAgent />
    </SafeAreaView>
  );
};

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: '#0a0a0f',
  },
});

export default App;
