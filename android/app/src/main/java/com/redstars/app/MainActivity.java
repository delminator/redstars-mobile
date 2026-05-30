package com.redstars.app;

import android.os.Bundle;
import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        // Doit être appelé AVANT super.onCreate(...) pour que Capacitor
        // pique le plugin lors de la construction du Bridge.
        registerPlugin(RedstarsHelperPlugin.class);
        super.onCreate(savedInstanceState);
    }
}
