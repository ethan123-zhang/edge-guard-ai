package com.sentinel.master;

import android.app.Activity;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.os.Bundle;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;

/**
 * 最小 WebView 壳：主界面复用局域网看板，只在"没配置"时显示设置页。
 *
 * 为什么不重写 UI：设计 §8 —— APK 与 Web 看板共用同一份页面与 API，
 * 手机浏览器、笔记本投屏、APK 三处看到的东西必须一致。
 *
 * 配置（服务器地址 + 设备 token）存 SharedPreferences；
 * 页面用 localStorage 保存 token，所以这里在页面加载完成后注入一次再刷新。
 */
public class MainActivity extends Activity {

    private static final String PREFS = "sentinel";
    private static final String KEY_URL = "server_url";
    private static final String KEY_TOKEN = "device_token";
    private static final String DEFAULT_URL = "http://192.168.5.11:8766/";

    private LinearLayout root;
    private WebView webView;
    private SharedPreferences prefs;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        setContentView(root);
        if (serverUrl().isEmpty() || token().isEmpty()) {
            showSettings();
        } else {
            showWebView();
        }
    }

    private String serverUrl() {
        return prefs.getString(KEY_URL, DEFAULT_URL);
    }

    private String token() {
        return prefs.getString(KEY_TOKEN, "");
    }

    // ------------------------------------------------------------ 设置页

    private void showSettings() {
        root.removeAllViews();
        webView = null;

        TextView title = new TextView(this);
        title.setText("哨兵管家 · 连接设置");
        title.setTextSize(22);
        title.setPadding(48, 96, 48, 24);
        root.addView(title);

        final EditText urlInput = new EditText(this);
        urlInput.setHint("服务器地址，例如 http://192.168.5.11:8766/");
        urlInput.setText(serverUrl());
        urlInput.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        root.addView(urlInput, params());

        final EditText tokenInput = new EditText(this);
        tokenInput.setHint("设备 token（服务端 --issue-token 生成）");
        tokenInput.setText(token());
        tokenInput.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        root.addView(tokenInput, params());

        Button save = new Button(this);
        save.setText("保存并连接");
        save.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View view) {
                String url = urlInput.getText().toString().trim();
                if (!url.startsWith("http://") && !url.startsWith("https://")) {
                    url = "http://" + url;
                }
                if (!url.endsWith("/")) {
                    url = url + "/";
                }
                prefs.edit().putString(KEY_URL, url)
                        .putString(KEY_TOKEN, tokenInput.getText().toString().trim())
                        .apply();
                showWebView();
            }
        });
        root.addView(save, params());
    }

    private LinearLayout.LayoutParams params() {
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.setMargins(48, 12, 48, 12);
        return lp;
    }

    // ------------------------------------------------------------ 看板

    private void showWebView() {
        root.removeAllViews();

        LinearLayout bar = new LinearLayout(this);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        bar.setGravity(Gravity.CENTER_VERTICAL);
        bar.setBackgroundColor(Color.parseColor("#171e26"));
        bar.setPadding(24, 8, 24, 8);

        TextView label = new TextView(this);
        label.setText("哨兵管家");
        label.setTextColor(Color.parseColor("#e7edf3"));
        label.setTextSize(16);
        bar.addView(label, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));

        Button settings = new Button(this);
        settings.setText("设置");
        settings.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View view) {
                showSettings();
            }
        });
        bar.addView(settings);
        root.addView(bar, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        webView = new WebView(this);
        WebSettings ws = webView.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setDomStorageEnabled(true);
        ws.setAllowFileAccess(false);
        webView.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView view, String url) {
                injectToken(view);
            }
        });
        root.addView(webView, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));
        webView.loadUrl(serverUrl());
    }

    private void injectToken(final WebView view) {
        String js = "if (localStorage.getItem('sentinel.token') !== " + jsString(token()) + ") {"
                + "localStorage.setItem('sentinel.server', " + jsString(serverUrl()) + ");"
                + "localStorage.setItem('sentinel.token', " + jsString(token()) + ");"
                + "location.reload(); }";
        view.evaluateJavascript(js, null);
    }

    private static String jsString(String value) {
        return "\"" + value.replace("\\", "\\\\").replace("\"", "\\\"") + "\"";
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
            return;
        }
        showSettings();
    }

    @Override
    protected void onDestroy() {
        if (webView != null) {
            webView.destroy();
            webView = null;
        }
        super.onDestroy();
    }
}