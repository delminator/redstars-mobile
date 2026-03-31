import type { CapacitorConfig } from '@capacitor/cli';

const config: CapacitorConfig = {
  appId: 'com.redstars.app.dev',
  appName: 'RedStars Dev',
  webDir: 'www',
  server: {
    // Development HTTPS URL
    url: 'https://dev.redstars.redlinks.fr',
  },
  android: {
    // Custom user agent to identify mobile app
    appendUserAgent: 'RedStarsApp/1.0-dev',
  },
  plugins: {
    SplashScreen: {
      launchShowDuration: 2000,
      backgroundColor: '#1a1a2e',
      showSpinner: false,
    },
  },
};

export default config;
