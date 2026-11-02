"""使用已有Android SDK离线构建隔离验收APK；调试签名仅存指定输出目录。"""

import argparse
import subprocess
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def build(sdk: Path, java: Path, output: Path) -> Path:
    """固定测试包与API30，不安装到任何设备；工具失败立即停止，输出保留供诊断。"""
    tools = sdk / "build-tools/30.0.2"
    android = sdk / "platforms/android-30/android.jar"
    source = Path(__file__).resolve().parents[1] / "tests/support/android_app"
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    classes, dex = output / "classes", output / "dex"
    classes.mkdir(exist_ok=True)
    dex.mkdir(exist_ok=True)

    def run(*args: str | Path) -> None:
        """参数数组调用固定工具；无shell拼接，错误直接传播。"""
        subprocess.run([str(arg) for arg in args], check=True, cwd=output)

    run(
        tools / "aapt2.exe",
        "link",
        "--manifest",
        source / "AndroidManifest.xml",
        "-I",
        android,
        "-o",
        output / "base.apk",
    )
    run(
        java / "javac.exe",
        "-encoding",
        "UTF-8",
        "-source",
        "8",
        "-target",
        "8",
        "-classpath",
        android,
        "-d",
        classes,
        source / "MainActivity.java",
    )
    jar = output / "classes.jar"
    with ZipFile(jar, "w", ZIP_DEFLATED) as archive:
        for path in classes.rglob("*.class"):
            archive.write(path, path.relative_to(classes).as_posix())
    run(
        java / "java.exe",
        "-cp",
        tools / "lib/d8.jar",
        "com.android.tools.r8.D8",
        "--lib",
        android,
        "--min-api",
        "23",
        "--output",
        dex,
        jar,
    )
    with ZipFile(output / "base.apk", "a", ZIP_DEFLATED) as archive:
        archive.write(dex / "classes.dex", "classes.dex")
    run(tools / "zipalign.exe", "-f", "4", output / "base.apk", output / "aligned.apk")
    key = output / "test-only.jks"
    if not key.exists():
        run(
            java / "keytool.exe",
            "-genkeypair",
            "-keystore",
            key,
            "-storepass",
            "android",
            "-keypass",
            "android",
            "-alias",
            "test",
            "-keyalg",
            "RSA",
            "-validity",
            "3650",
            "-dname",
            "CN=Adam Emulator Test",
            "-noprompt",
        )
    apk = output / "adam-practice.apk"
    signer = tools / "lib/apksigner.jar"
    run(
        java / "java.exe",
        "-jar",
        signer,
        "sign",
        "--ks",
        key,
        "--ks-pass",
        "pass:android",
        "--key-pass",
        "pass:android",
        "--out",
        apk,
        output / "aligned.apk",
    )
    run(java / "java.exe", "-jar", signer, "verify", "--verbose", apk)
    return apk


def main() -> None:
    """显式指定已有SDK、JDK bin与输出目录，不下载或改变全局开发环境。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--java", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(build(args.sdk, args.java, args.output))


if __name__ == "__main__":
    main()
