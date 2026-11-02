package com.adam.practice;

import android.app.Activity;
import android.os.Bundle;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import org.json.JSONObject;
import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;

/** 隔离模拟器验收应用；记录保存在应用私有文件，不申请网络或设备权限。 */
public final class MainActivity extends Activity {
    private TextView result;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        layout.setPadding(24, 40, 24, 24);
        TextView title = new TextView(this);
        title.setText("Adam Practice - Local Record");
        title.setTextSize(24);
        layout.addView(title);
        EditText name = new EditText(this);
        name.setId(1001);
        name.setSingleLine(true);
        name.setHint("Record name");
        name.setContentDescription("record-name");
        layout.addView(name);
        EditText note = new EditText(this);
        note.setId(1002);
        note.setSingleLine(true);
        note.setHint("Record note");
        note.setContentDescription("record-note");
        layout.addView(note);
        Button save = new Button(this);
        save.setText("Save record");
        save.setContentDescription("save-record");
        layout.addView(save);
        result = new TextView(this);
        result.setContentDescription("saved-result");
        layout.addView(result);
        setContentView(layout);
        showSaved();
        save.setOnClickListener(view -> {
            if (name.getText().toString().trim().isEmpty()) {
                result.setText("Name is required");
                return;
            }
            try {
                JSONObject record = new JSONObject();
                record.put("name", name.getText().toString());
                record.put("note", note.getText().toString());
                record.put("savedAt", System.currentTimeMillis());
                File temporary = new File(getFilesDir(), "record.tmp");
                try (FileOutputStream output = new FileOutputStream(temporary)) {
                    output.write(record.toString().getBytes(StandardCharsets.UTF_8));
                    output.getFD().sync();
                }
                if (!temporary.renameTo(new File(getFilesDir(), "record.json"))) {
                    throw new java.io.IOException("record replace failed");
                }
                showSaved();
            } catch (Exception error) {
                result.setText("Save failed: " + error.getClass().getSimpleName());
            }
        });
    }

    private void showSaved() {
        File path = new File(getFilesDir(), "record.json");
        try {
            result.setText(path.exists() ? "Saved: " + new String(Files.readAllBytes(path.toPath()), StandardCharsets.UTF_8) : "No saved record");
        } catch (Exception error) {
            result.setText("Read failed: " + error.getClass().getSimpleName());
        }
    }
}
