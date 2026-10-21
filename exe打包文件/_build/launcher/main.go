// 智能考场 AI 巡考系统 · 单文件启动器
//
// 本 exe = 启动器本体 + 追加的 payload.zip + 24 字节尾部索引。
// 首次运行把 payload 解压到 %LOCALAPPDATA%\智能考场AI巡考系统\app-<版本>，
// 之后直接复用；最后用解压目录里的 runtime\pythonw.exe 打开总控台.py。
package main

import (
	"archive/zip"
	"bufio"
	"bytes"
	"crypto/aes"
	"crypto/cipher"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/binary"
	"encoding/hex"
	"errors"
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
	trailerMagic = "ZKSFX001" // 旧版：明文 zip 尾部 24 字节
	trailerSize  = 24
	insMagic     = "ZKINS002" // 新版：加密 payload 尾部 40 字节
	insTrailer   = 40
	encMagic     = "ZKENC003"
	maxChunk     = 64 << 20
	markerName   = ".payload_ready"
	appTitle     = "智能考场AI巡考系统"
	installSalt  = "EDY_POWERQ-2026-install"
	installHash  = "a797785ced94240c008b5352f7362ae15be1336377ea478a274242b58cb72a0d"
	entryName    = "总控台.py"
)

// 加密版构建时通过 -ldflags "-X main.keyBlob=..." 注入（已异或 0x5A 的密钥材料）。
var keyBlob = ""

var (
	kernel32 = syscall.NewLazyDLL("kernel32.dll")
	user32   = syscall.NewLazyDLL("user32.dll")

	procSetConsoleOutputCP = kernel32.NewProc("SetConsoleOutputCP")
	procSetConsoleTitleW   = kernel32.NewProc("SetConsoleTitleW")
	procSetFileAttributesW = kernel32.NewProc("SetFileAttributesW")
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

func pbkdf2SHA256(password, salt []byte, iter, keyLen int) []byte {
	const hLen = 32
	blocks := (keyLen + hLen - 1) / hLen
	out := make([]byte, 0, blocks*hLen)
	for block := 1; block <= blocks; block++ {
		mac := hmac.New(sha256.New, password)
		mac.Write(salt)
		var idx [4]byte
		binary.BigEndian.PutUint32(idx[:], uint32(block))
		mac.Write(idx[:])
		u := mac.Sum(nil)
		t := append([]byte(nil), u...)
		for i := 1; i < iter; i++ {
			mac = hmac.New(sha256.New, password)
			mac.Write(u)
			u = mac.Sum(nil)
			for j := range t {
				t[j] ^= u[j]
			}
		}
		out = append(out, t...)
	}
	return out[:keyLen]
}

func verifyInstallPassword(pw string, salt []byte, hashHex string) bool {
	want, err := hex.DecodeString(hashHex)
	if err != nil {
		return false
	}
	got := pbkdf2SHA256([]byte(pw), salt, 120000, len(want))
	return subtle.ConstantTimeCompare(got, want) == 1
}

func canWriteDir(dir string) bool {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return false
	}
	f, err := os.CreateTemp(dir, ".zk_auth_test_*")
	if err != nil {
		return false
	}
	name := f.Name()
	f.Close()
	os.Remove(name)
	return true
}

func installAuthPath(exePath string) string {
	base := os.Getenv("LOCALAPPDATA")
	if base == "" {
		base = filepath.Join(os.Getenv("USERPROFILE"), "AppData", "Local")
	}
	cands := []string{
		filepath.Join(base, appTitle),
		filepath.Dir(exePath),
		os.TempDir(),
	}
	for _, d := range cands {
		if canWriteDir(d) {
			return filepath.Join(d, ".install_auth")
		}
	}
	return filepath.Join(os.TempDir(), ".install_auth")
}
func hideFile(path string) {
	if p, err := syscall.UTF16PtrFromString(path); err == nil {
		procSetFileAttributesW.Call(uintptr(unsafe.Pointer(p)), 0x2|0x4)
	}
}

func promptInstallPassword() (string, error) {
	form := `
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$form = New-Object System.Windows.Forms.Form
$form.Text = '智能考场 AI 巡考系统 · 安装密码'
$form.ClientSize = New-Object System.Drawing.Size(430, 155)
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$label = New-Object System.Windows.Forms.Label
$label.Text = '首次启动，请输入安装密码：'
$label.AutoSize = $true
$label.Location = New-Object System.Drawing.Point(18, 18)
$tb = New-Object System.Windows.Forms.TextBox
$tb.UseSystemPasswordChar = $true
$tb.Width = 390
$tb.Location = New-Object System.Drawing.Point(18, 46)
$ok = New-Object System.Windows.Forms.Button
$ok.Text = '确定'
$ok.DialogResult = 'OK'
$ok.Location = New-Object System.Drawing.Point(228, 92)
$cancel = New-Object System.Windows.Forms.Button
$cancel.Text = '取消'
$cancel.DialogResult = 'Cancel'
$cancel.Location = New-Object System.Drawing.Point(318, 92)
$form.Controls.AddRange(@($label, $tb, $ok, $cancel))
$form.AcceptButton = $ok
$form.CancelButton = $cancel
$r = $form.ShowDialog()
if ($r -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $tb.Text }
`
	cmd := exec.Command("powershell.exe", "-NoProfile", "-STA", "-WindowStyle", "Hidden", "-Command", form)
	out, err := cmd.Output()
	if err == nil {
		return strings.TrimSpace(string(out)), nil
	}
	// PowerShell 不可用时的控制台兜底
	fmt.Print("请输入安装密码：")
	line, rerr := bufio.NewReader(os.Stdin).ReadString('\n')
	if rerr != nil && strings.TrimSpace(line) == "" {
		return "", errors.New("已取消")
	}
	return strings.TrimSpace(line), nil
}

func authSig(salt, hash []byte) []byte {
	mac := hmac.New(sha256.New, []byte("EDY_POWERQ-2026-auth-sign"))
	mac.Write(salt)
	mac.Write(hash)
	return mac.Sum(nil)
}

func ensureInstallAuth(exePath string) error {
	auth := installAuthPath(exePath)
	if data, err := os.ReadFile(auth); err == nil {
		lines := strings.Split(strings.ReplaceAll(string(data), "\r\n", "\n"), "\n")
		if len(lines) >= 4 && strings.TrimSpace(lines[0]) == "v3" {
			salt, e1 := hex.DecodeString(strings.TrimSpace(lines[1]))
			hash, e2 := hex.DecodeString(strings.TrimSpace(lines[2]))
			sig, e3 := hex.DecodeString(strings.TrimSpace(lines[3]))
			if e1 == nil && e2 == nil && e3 == nil &&
				subtle.ConstantTimeCompare(authSig(salt, hash), sig) == 1 {
				return nil // 首次已录入，后续不再重复输入
			}
		}
	}
	for attempt := 0; attempt < 3; attempt++ {
		pw := os.Getenv("EXAM_INSTALL_PASSWORD")
		if pw == "" {
			var err error
			pw, err = promptInstallPassword()
			if err != nil {
				return err
			}
		}
		if !verifyInstallPassword(pw, []byte(installSalt), installHash) {
			if attempt < 2 {
				msgBox(appTitle+" · 安装密码错误", "安装密码不正确，请重试。", 0x10)
			}
			continue
		}
		salt := make([]byte, 16)
		if _, err := rand.Read(salt); err != nil {
			return err
		}
		hash := pbkdf2SHA256([]byte(pw), salt, 120000, 32)
		sig := authSig(salt, hash)
		body := "v3\n" + hex.EncodeToString(salt) + "\n" + hex.EncodeToString(hash) + "\n" +
			hex.EncodeToString(sig) + "\n"
		if err := os.WriteFile(auth, []byte(body), 0o600); err != nil {
			return fmt.Errorf("无法保存安装密码：%w", err)
		}
		hideFile(auth)
		return nil
	}
	return errors.New("安装密码不正确")
}
func main() {
	setConsoleUTF8()
	fmt.Println("智能考场 AI 巡考系统 · 版权 © 2026 EDY_POWERQ")

	exePath, err := os.Executable()
	if err != nil {
		fatal("无法定位自身路径：" + err.Error())
	}
	if err := ensureInstallAuth(exePath); err != nil {
		fatal("安装密码：" + err.Error())
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

	var payloadOff, payloadSize int64
	var encrypted bool
	var salt, stubHash []byte
	// 新版：40 字节尾部（加密 payload）；旧版：24 字节尾部（明文 zip）。
	tr40 := make([]byte, insTrailer)
	if fileSize >= insTrailer {
		_, _ = f.ReadAt(tr40, fileSize-insTrailer)
	}
	if string(tr40[:8]) == insMagic {
		encrypted = true
		payloadOff = int64(binary.LittleEndian.Uint64(tr40[8:16]))
		payloadSize = int64(binary.LittleEndian.Uint64(tr40[16:24]))
		salt = append([]byte(nil), tr40[24:40]...)
		if payloadOff <= 0 || payloadSize <= 0 || payloadOff+payloadSize+insTrailer != fileSize {
			fatal("加密 payload 索引损坏，请重新下载。")
		}
		h := sha256.New()
		if _, err := f.Seek(0, io.SeekStart); err != nil {
			fatal("读取安装包失败：" + err.Error())
		}
		if _, err := io.CopyN(h, f, payloadOff); err != nil {
			fatal("校验安装包失败：" + err.Error())
		}
		stubHash = h.Sum(nil)
	} else {
		tr := make([]byte, trailerSize)
		if _, err := f.ReadAt(tr, fileSize-trailerSize); err != nil {
			fatal("读取尾部索引失败：" + err.Error())
		}
		if string(tr[:8]) != trailerMagic {
			fatal("这不是有效的单文件安装包（缺少 payload 索引）。")
		}
		payloadOff = int64(binary.LittleEndian.Uint64(tr[8:16]))
		payloadSize = int64(binary.LittleEndian.Uint64(tr[16:24]))
		if payloadOff <= 0 || payloadSize <= 0 || payloadOff+payloadSize > fileSize-trailerSize {
			fatal("payload 索引损坏，请重新下载。")
		}
	}

	version := fmt.Sprintf("%x", fileSize)

	base := os.Getenv("LOCALAPPDATA")
	if base == "" {
		base = os.TempDir()
	}
	target := filepath.Join(base, appTitle, "app-"+version)
	// 上一次可能已回退到 %TEMP% 或 exe 所在目录：优先复用已解压好的那份，
	// 避免每次启动都重新解密/解压 800+ MB。
	if ready := existingTarget(base, version, exePath); ready != "" {
		target = ready
	}

	if !isReady(target) {
		var zr *zip.Reader
		var zrc io.Closer
		tmpZip := ""
		if encrypted {
			key, err := deriveKey(salt, stubHash)
			if err != nil {
				fatal("安装包密钥校验失败：" + err.Error())
			}
			var tmp *os.File
			var tmpErr error
			for _, d := range []string{filepath.Join(base, appTitle), os.TempDir(), filepath.Dir(exePath)} {
				_ = os.MkdirAll(d, 0o755)
				tmp, tmpErr = os.CreateTemp(d, "zk_payload_*.tmp")
				if tmpErr == nil {
					break
				}
			}
			if tmpErr != nil {
				// 临时目录不可写（常见于桌面/受限目录/杀软拦截）：改用内存解密。
				// 只在首次运行发生，不影响后续启动速度。
				fmt.Printf("临时目录不可写，改用内存解密（约 %d MB）...\n", payloadSize>>20)
				mem, err := decryptToMemory(f, payloadOff, payloadSize, key)
				if err != nil {
					fatal("内存解密失败：" + err.Error())
				}
				zr, err = zip.NewReader(bytes.NewReader(mem), int64(len(mem)))
				if err != nil {
					fatal("解密后的 payload 无法读取：" + err.Error())
				}
			} else {
				tmpZip = tmp.Name()
				tmp.Close()
				fmt.Printf("首次运行：正在解密并解压运行环境到\n  %s\n（加密载荷 %d MB；只需这一次，以后直接启动）\n\n",
					target, payloadSize>>20)
				if err := decryptToFile(f, payloadOff, payloadSize, key, tmpZip); err != nil {
					os.Remove(tmpZip)
					fatal("解密失败：" + err.Error())
				}
				rc, err := zip.OpenReader(tmpZip)
				if err != nil {
					os.Remove(tmpZip)
					fatal("解密后的 payload 无法读取：" + err.Error())
				}
				zr = &rc.Reader
				zrc = rc
				defer os.Remove(tmpZip)
				defer zrc.Close()
			}
		} else {
			sec := io.NewSectionReader(f, payloadOff, payloadSize)
			var err error
			zr, err = zip.NewReader(sec, payloadSize)
			if err != nil {
				fatal("payload 压缩包无法读取：" + err.Error())
			}
			fmt.Printf("首次运行：正在解压运行环境到\n  %s\n（压缩包 %d MB；只需这一次，以后直接启动）\n\n",
				target, payloadSize>>20)
		}
		var unpacked uint64
		for _, zf := range zr.File {
			unpacked += zf.UncompressedSize64
		}
		fmt.Printf("解压后约 %d MB ...\n", unpacked>>20)
		if err := extractWithFallback(zr, base, version, exePath, &target); err != nil {
			fatal("解压失败：" + err.Error())
		}
		hardenApp(target)
		hideDir(target)
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

func secret() ([]byte, error) {
	if keyBlob == "" {
		return nil, errors.New("加密安装包密钥材料缺失")
	}
	b, err := hex.DecodeString(keyBlob)
	if err != nil {
		return nil, err
	}
	for i := range b {
		b[i] ^= 0x5A
	}
	if len(b) != 32 {
		return nil, fmt.Errorf("密钥长度错误：%d", len(b))
	}
	return b, nil
}

func deriveKey(salt, stubHash []byte) ([]byte, error) {
	sec, err := secret()
	if err != nil {
		return nil, err
	}
	h := sha256.New()
	h.Write(sec)
	h.Write(salt)
	h.Write(stubHash)
	return h.Sum(nil), nil
}

func decryptToFile(f *os.File, offset, size int64, key []byte, outPath string) error {
	block, err := aes.NewCipher(key)
	if err != nil {
		return err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return err
	}
	if _, err := f.Seek(offset, io.SeekStart); err != nil {
		return err
	}
	head := make([]byte, 20)
	if _, err := io.ReadFull(f, head); err != nil {
		return err
	}
	if string(head[:8]) != encMagic {
		return errors.New("加密数据标识不正确")
	}
	chunkSize := binary.LittleEndian.Uint32(head[8:12])
	if chunkSize == 0 || chunkSize > maxChunk {
		return fmt.Errorf("分块大小异常：%d", chunkSize)
	}
	noncePrefix := head[12:20]
	out, err := os.OpenFile(outPath, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	defer out.Close()

	remaining := size - 20
	var counter uint32
	var done int64
	fmt.Print("正在解密 ... ")
	for remaining > 0 {
		if remaining < 4 {
			return errors.New("密文块长度不完整")
		}
		var lb [4]byte
		if _, err := io.ReadFull(f, lb[:]); err != nil {
			return err
		}
		n := int64(binary.LittleEndian.Uint32(lb[:]))
		remaining -= 4
		if n <= 16 || n > int64(chunkSize)+16 || n > remaining {
			return fmt.Errorf("密文块长度异常：%d", n)
		}
		ct := make([]byte, n)
		if _, err := io.ReadFull(f, ct); err != nil {
			return err
		}
		remaining -= n
		nonce := make([]byte, 12)
		copy(nonce, noncePrefix)
		binary.BigEndian.PutUint32(nonce[8:], counter)
		plain, err := gcm.Open(nil, nonce, ct, nil)
		if err != nil {
			return errors.New("解密校验失败：安装包被修改或密钥不匹配")
		}
		if _, err := out.Write(plain); err != nil {
			return err
		}
		counter++
		done += n + 4
		if done*100/(size-20) != (done-int64(n)-4)*100/(size-20) {
			fmt.Printf("\r正在解密 ... %d%%", done*100/(size-20))
		}
	}
	fmt.Println("\r正在解密 ... 100%")
	return out.Sync()
}

func decryptToMemory(f *os.File, offset, size int64, key []byte) ([]byte, error) {
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, err
	}
	if _, err := f.Seek(offset, io.SeekStart); err != nil {
		return nil, err
	}
	head := make([]byte, 20)
	if _, err := io.ReadFull(f, head); err != nil {
		return nil, err
	}
	if string(head[:8]) != encMagic {
		return nil, errors.New("加密数据标识不正确")
	}
	chunkSize := binary.LittleEndian.Uint32(head[8:12])
	if chunkSize == 0 || chunkSize > maxChunk {
		return nil, fmt.Errorf("分块大小异常：%d", chunkSize)
	}
	noncePrefix := head[12:20]
	var buf bytes.Buffer
	remaining := size - 20
	var counter uint32
	for remaining > 0 {
		if remaining < 4 {
			return nil, errors.New("密文块长度不完整")
		}
		var lb [4]byte
		if _, err := io.ReadFull(f, lb[:]); err != nil {
			return nil, err
		}
		n := int64(binary.LittleEndian.Uint32(lb[:]))
		remaining -= 4
		if n <= 16 || n > int64(chunkSize)+16 || n > remaining {
			return nil, fmt.Errorf("密文块长度异常：%d", n)
		}
		ct := make([]byte, n)
		if _, err := io.ReadFull(f, ct); err != nil {
			return nil, err
		}
		remaining -= n
		nonce := make([]byte, 12)
		copy(nonce, noncePrefix)
		binary.BigEndian.PutUint32(nonce[8:], counter)
		plain, err := gcm.Open(nil, nonce, ct, nil)
		if err != nil {
			return nil, errors.New("解密校验失败：安装包被修改或密钥不匹配")
		}
		if _, err := buf.Write(plain); err != nil {
			return nil, err
		}
		counter++
	}
	return buf.Bytes(), nil
}

func hardenApp(appDir string) {
	py := filepath.Join(appDir, "runtime", "python.exe")
	if _, err := os.Stat(py); err != nil {
		py = filepath.Join(appDir, "runtime", "Scripts", "python.exe")
	}
	script := filepath.Join(appDir, "_harden.py")
	if _, err := os.Stat(py); err != nil {
		return
	}
	if _, err := os.Stat(script); err != nil {
		return
	}
	fmt.Print("正在硬化源码（pyc + loader）... ")
	cmd := exec.Command(py, script, appDir)
	cmd.Dir = appDir
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Run(); err != nil {
		fmt.Println("提示：源码硬化未完成（不影响运行）：", err)
		return
	}
	fmt.Println("完成")
}

func hideDir(dir string) {
	if p, err := syscall.UTF16PtrFromString(dir); err == nil {
		// FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM
		procSetFileAttributesW.Call(uintptr(unsafe.Pointer(p)), 0x2|0x4)
	}
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
