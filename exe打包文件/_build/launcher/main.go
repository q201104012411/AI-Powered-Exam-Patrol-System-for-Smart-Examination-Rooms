// 智能考场 AI 巡考系统 · 单文件启动器
//
// 本 exe = 启动器本体 + 追加的 payload.zip + 24 字节尾部索引。
// 首次运行把 payload 解压到 %LOCALAPPDATA%\智能考场AI巡考系统\app-<版本>，
// 之后直接复用；最后用解压目录里的 runtime\pythonw.exe 打开总控台.py。
package main

import (
	"archive/zip"
	"encoding/binary"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"
	"unsafe"
)

const (
	trailerMagic = "ZKSFX001" // 8 字节
	trailerSize  = 24         // magic(8) + offset(8) + size(8)
	markerName   = ".payload_ready"
	appTitle     = "智能考场AI巡考系统"
	entryName    = "总控台.py"
)

var (
	kernel32 = syscall.NewLazyDLL("kernel32.dll")
	user32   = syscall.NewLazyDLL("user32.dll")

	procSetConsoleOutputCP = kernel32.NewProc("SetConsoleOutputCP")
	procSetConsoleTitleW   = kernel32.NewProc("SetConsoleTitleW")
	procMessageBoxW        = user32.NewProc("MessageBoxW")
)

func setConsoleUTF8() {
	procSetConsoleOutputCP.Call(65001)
	t, _ := syscall.UTF16PtrFromString(appTitle + " · 正在准备运行环境")
	procSetConsoleTitleW.Call(uintptr(unsafe.Pointer(t)))
}

func msgBox(title, text string, flags uintptr) {
	t, _ := syscall.UTF16PtrFromString(text)
	c, _ := syscall.UTF16PtrFromString(title)
	procMessageBoxW.Call(0,
		uintptr(unsafe.Pointer(t)),
		uintptr(unsafe.Pointer(c)),
		flags)
}

func fatal(text string) {
	fmt.Fprintln(os.Stderr, text)
	msgBox(appTitle+" · 启动失败", text, 0x10) // MB_ICONERROR
	os.Exit(1)
}

func main() {
	setConsoleUTF8()

	exePath, err := os.Executable()
	if err != nil {
		fatal("无法定位自身路径：" + err.Error())
	}
	f, err := os.Open(exePath)
	if err != nil {
		fatal("无法打开自身文件：" + err.Error())
	}
	defer f.Close()

	st, err := f.Stat()
	if err != nil || st.Size() < trailerSize {
		fatal("exe 文件不完整。")
	}
	fileSize := st.Size()

	tr := make([]byte, trailerSize)
	if _, err := f.ReadAt(tr, fileSize-trailerSize); err != nil {
		fatal("读取尾部索引失败：" + err.Error())
	}
	if string(tr[:8]) != trailerMagic {
		fatal("这不是有效的单文件安装包（缺少 payload 索引）。")
	}
	payloadOff := int64(binary.LittleEndian.Uint64(tr[8:16]))
	payloadSize := int64(binary.LittleEndian.Uint64(tr[16:24]))
	if payloadOff <= 0 || payloadSize <= 0 || payloadOff+payloadSize > fileSize-trailerSize {
		fatal("payload 索引损坏，请重新下载。")
	}

	version := fmt.Sprintf("%x", payloadSize)

	base := os.Getenv("LOCALAPPDATA")
	if base == "" {
		base = os.TempDir()
	}
	target := filepath.Join(base, appTitle, "app-"+version)
	// 上一次可能已回退到 %TEMP% 或 exe 所在目录：优先复用已解压好的那份，
	// 避免每次启动都重新解压 800+ MB。
	if ready := existingTarget(base, version, exePath); ready != "" {
		target = ready
	}

	if !isReady(target) {
		sec := io.NewSectionReader(f, payloadOff, payloadSize)
		zr, err := zip.NewReader(sec, payloadSize)
		if err != nil {
			fatal("payload 压缩包无法读取：" + err.Error())
		}
		var unpacked uint64
		for _, zf := range zr.File {
			unpacked += zf.UncompressedSize64
		}
		fmt.Printf("首次运行：正在解压运行环境到\n  %s\n（压缩包 %d MB，解压后约 %d MB；只需这一次，以后直接启动）\n\n",
			target, payloadSize>>20, unpacked>>20)
		if err := extractWithFallback(zr, base, version, exePath, &target); err != nil {
			fatal("解压失败：" + err.Error())
		}
		fmt.Println("\n解压完成。")
	}

	pyw := filepath.Join(target, "runtime", "pythonw.exe")
	if _, err := os.Stat(pyw); err != nil {
		pyw = filepath.Join(target, "runtime", "Scripts", "pythonw.exe")
	}
	entry := filepath.Join(target, entryName)
	if _, err := os.Stat(pyw); err != nil {
		fatal("运行环境缺少 runtime\\pythonw.exe：\n" + pyw)
	}
	if _, err := os.Stat(entry); err != nil {
		fatal("缺少总控台.py：\n" + entry)
	}

	cmd := exec.Command(pyw, entry)
	cmd.Dir = target
	if err := cmd.Start(); err != nil {
		fatal("启动总控台失败：" + err.Error())
	}
	_ = cmd.Process.Release()

	fmt.Println("总控台已启动。")
	time.Sleep(600 * time.Millisecond)
}

// 解压候选目录：%LOCALAPPDATA% -> %TEMP% -> exe 所在目录。
// 受限账户/沙箱/组策略可能连 %TEMP% 都写不了，此时用 exe 同目录兜底。
func targetCandidates(base, version, exePath string) []string {
	raw := []string{
		base,
		os.TempDir(),
		filepath.Dir(exePath),
	}
	seen := map[string]bool{}
	out := make([]string, 0, len(raw))
	for _, b := range raw {
		if b == "" || seen[b] {
			continue
		}
		seen[b] = true
		out = append(out, filepath.Join(b, appTitle, "app-"+version))
	}
	return out
}

func existingTarget(base, version, exePath string) string {
	for _, t := range targetCandidates(base, version, exePath) {
		if isReady(t) {
			return t
		}
	}
	return ""
}

// extractWithFallback 依次尝试候选目录，成功时把实际使用的 target 带回。
func extractWithFallback(zr *zip.Reader, base, version, exePath string, target *string) error {
	cands := targetCandidates(base, version, exePath)
	var lastErr error
	for i, t := range cands {
		if i == 0 {
			lastErr = extract(zr, t)
		} else {
			fmt.Printf("\n上一目录不可写（%v），改试：\n  %s\n", lastErr, t)
			lastErr = extract(zr, t)
		}
		if lastErr == nil {
			*target = t
			return nil
		}
		os.RemoveAll(t + ".tmp")
	}
	return lastErr
}

func isReady(dir string) bool {
	for _, rel := range [][]string{
		{markerName},
		{"模块清单.json"},
		{entryName},
		{"runtime", "pythonw.exe"},
	} {
		if _, err := os.Stat(filepath.Join(append([]string{dir}, rel...)...)); err != nil {
			return false
		}
	}
	return true
}

func extract(zr *zip.Reader, target string) error {
	tmp := target + ".tmp"
	if err := os.RemoveAll(tmp); err != nil {
		return err
	}
	if err := os.MkdirAll(tmp, 0o755); err != nil {
		return err
	}

	total := len(zr.File)
	for i, zf := range zr.File {
		name := strings.ReplaceAll(zf.Name, "\\", "/")
		clean := filepath.Clean(filepath.FromSlash(name))
		if clean == "." || strings.HasPrefix(clean, "..") || filepath.IsAbs(clean) {
			continue
		}
		dst := filepath.Join(tmp, clean)
		if zf.FileInfo().IsDir() || strings.HasSuffix(name, "/") {
			if err := os.MkdirAll(dst, 0o755); err != nil {
				return err
			}
			continue
		}
		if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
			return err
		}
		if err := writeEntry(zf, dst); err != nil {
			return fmt.Errorf("%s: %w", name, err)
		}
		if i%400 == 0 {
			fmt.Printf("\r  已解压 %d/%d 个文件 ...", i+1, total)
		}
	}
	fmt.Printf("\r  已解压 %d/%d 个文件 ...  ", total, total)

	if err := os.WriteFile(filepath.Join(tmp, markerName), []byte("ok"), 0o644); err != nil {
		return err
	}

	// 旧版本残留：只在目标不可用时清理，避免误删已有数据。
	if err := os.RemoveAll(target); err != nil {
		return err
	}
	return os.Rename(tmp, target)
}

func writeEntry(zf *zip.File, dst string) error {
	rc, err := zf.Open()
	if err != nil {
		return err
	}
	defer rc.Close()

	out, err := os.OpenFile(dst, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	if _, err := io.CopyBuffer(out, rc, make([]byte, 1<<20)); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}
