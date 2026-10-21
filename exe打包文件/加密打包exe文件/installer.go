// 智能考场 AI 巡考系统 · 加密安装包（Windows amd64）
//
// 结构：installer.exe（本程序） + payload.enc（AES-256-GCM 分块密文） + 40 字节尾部索引。
// 安装时：
//  1. 选择安装目录；
//  2. 用“内置混淆密钥 + 随机 salt + 本 exe 头部哈希”派生密钥；
//  3. 分块解密 payload，解压到 <安装目录>\app；
//  4. 生成启动 bat / 桌面快捷方式，并可立即启动。
//
// 说明：payload 在安装包内始终是密文；安装后为正常 Python 运行时文件。
// 这是“提高逆向/提取门槛”的工程措施，不等同于不可破解的 DRM。
package main

import (
	"archive/zip"
	"crypto/aes"
	"crypto/cipher"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"unicode/utf16"
	"unsafe"
)

const (
	appTitle    = "智能考场 AI 巡考系统"
	trailerSize = 40
	encMagic    = "ZKENC003"
	insMagic    = "ZKINS002"
	maxChunk    = 64 << 20
)

// 由构建脚本通过 -ldflags "-X main.keyBlob=..." 注入（已与 0x5A 异或的密钥材料）。
var keyBlob = ""

var (
	kernel32 = syscall.NewLazyDLL("kernel32.dll")
	user32   = syscall.NewLazyDLL("user32.dll")
	shell32  = syscall.NewLazyDLL("shell32.dll")
	ole32    = syscall.NewLazyDLL("ole32.dll")

	procIsDebuggerPresent    = kernel32.NewProc("IsDebuggerPresent")
	procSetConsoleOutputCP   = kernel32.NewProc("SetConsoleOutputCP")
	procSetConsoleTitleW     = kernel32.NewProc("SetConsoleTitleW")
	procMessageBoxW          = user32.NewProc("MessageBoxW")
	procSendMessageW         = user32.NewProc("SendMessageW")
	procSHBrowseForFolderW   = shell32.NewProc("SHBrowseForFolderW")
	procSHGetPathFromIDListW = shell32.NewProc("SHGetPathFromIDListW")
	procCoTaskMemFree        = ole32.NewProc("CoTaskMemFree")
)

type browseInfoW struct {
	hwndOwner      uintptr
	pidlRoot       uintptr
	pszDisplayName *uint16
	lpszTitle      *uint16
	ulFlags        uint32
	lpfn           uintptr
	lParam         uintptr
	iImage         int32
}

func utf16Ptr(s string) *uint16 {
	p, _ := syscall.UTF16PtrFromString(s)
	return p
}

func msgBox(title, text string, flags uint32) int {
	r, _, _ := procMessageBoxW.Call(0,
		uintptr(unsafe.Pointer(utf16Ptr(text))),
		uintptr(unsafe.Pointer(utf16Ptr(title))),
		uintptr(flags))
	return int(r)
}

func setConsole() {
	procSetConsoleOutputCP.Call(65001)
	procSetConsoleTitleW.Call(uintptr(unsafe.Pointer(utf16Ptr(appTitle + " · 安装程序"))))
}

var browseCallback = syscall.NewCallback(func(hwnd uintptr, msg uint32, lparam, data uintptr) uintptr {
	if msg == 1 { // BFFM_INITIALIZED
		procSendMessageW.Call(hwnd, 0x0467, 1, data) // BFFM_SETSELECTIONW
	}
	return 0
})

func browseFolder(title, initial string) (string, bool) {
	var display [260]uint16
	bi := browseInfoW{
		pszDisplayName: &display[0],
		lpszTitle:      utf16Ptr(title),
		ulFlags:        0x00000051, // RETURNONLYFSDIRS | EDITBOX | NEWDIALOGSTYLE
		lpfn:           browseCallback,
		iImage:         0,
	}
	initPtr := utf16Ptr(initial)
	bi.lParam = uintptr(unsafe.Pointer(initPtr))
	pidl, _, _ := procSHBrowseForFolderW.Call(uintptr(unsafe.Pointer(&bi)))
	runtime.KeepAlive(initPtr)
	if pidl == 0 {
		return "", false
	}
	var buf [260]uint16
	ok, _, _ := procSHGetPathFromIDListW.Call(pidl, uintptr(unsafe.Pointer(&buf[0])))
	procCoTaskMemFree.Call(pidl)
	if ok == 0 {
		return "", false
	}
	return syscall.UTF16ToString(buf[:]), true
}

func fatal(title, text string) {
	fmt.Fprintln(os.Stderr, text)
	msgBox(title, text, 0x10)
	os.Exit(1)
}

func secret() ([]byte, error) {
	if keyBlob == "" {
		return nil, errors.New("安装包密钥材料缺失（构建参数错误）")
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

type envelope struct {
	offset   int64
	size     int64
	salt     []byte
	stubHash []byte
}

func readEnvelope(exePath string) (*envelope, error) {
	f, err := os.Open(exePath)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil {
		return nil, err
	}
	if st.Size() < trailerSize {
		return nil, errors.New("安装包不完整")
	}
	tr := make([]byte, trailerSize)
	if _, err := f.ReadAt(tr, st.Size()-trailerSize); err != nil {
		return nil, err
	}
	if string(tr[:8]) != insMagic {
		return nil, errors.New("安装包尾部标识不正确")
	}
	off := int64(binary.LittleEndian.Uint64(tr[8:16]))
	size := int64(binary.LittleEndian.Uint64(tr[16:24]))
	salt := append([]byte(nil), tr[24:40]...)
	if off <= 0 || size <= 0 || off+size+trailerSize != st.Size() {
		return nil, errors.New("安装包长度校验失败")
	}
	h := sha256.New()
	if _, err := io.CopyN(h, f, off); err != nil {
		return nil, err
	}
	return &envelope{offset: off, size: size, salt: salt, stubHash: h.Sum(nil)}, nil
}

func decryptToFile(exePath string, env *envelope, outPath string) error {
	key, err := deriveKey(env.salt, env.stubHash)
	if err != nil {
		return err
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return err
	}
	f, err := os.Open(exePath)
	if err != nil {
		return err
	}
	defer f.Close()
	if _, err := f.Seek(env.offset, io.SeekStart); err != nil {
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

	remaining := env.size - 20
	var counter uint32
	var done int64
	fmt.Print("正在解密安装数据 ... ")
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
		if done*100/(env.size-20) != (done-int64(n)-4)*100/(env.size-20) {
			fmt.Printf("\r正在解密安装数据 ... %d%%", done*100/(env.size-20))
		}
	}
	fmt.Println("\r正在解密安装数据 ... 100%")
	return out.Sync()
}

func extractZip(zipPath, appDir string) error {
	zr, err := zip.OpenReader(zipPath)
	if err != nil {
		return fmt.Errorf("安装数据不是有效 zip：%w", err)
	}
	defer zr.Close()
	root := filepath.Clean(appDir)
	for _, zf := range zr.File {
		name := filepath.Clean(strings.ReplaceAll(zf.Name, "\\", "/"))
		if name == "." || filepath.IsAbs(name) || strings.HasPrefix(name, "..") {
			return fmt.Errorf("压缩包内路径不安全：%s", zf.Name)
		}
		dest := filepath.Join(root, name)
		if dest != root && !strings.HasPrefix(dest, root+string(os.PathSeparator)) {
			return fmt.Errorf("压缩包内路径越界：%s", zf.Name)
		}
		if zf.FileInfo().IsDir() {
			if err := os.MkdirAll(dest, 0755); err != nil {
				return err
			}
			continue
		}
		if err := os.MkdirAll(filepath.Dir(dest), 0755); err != nil {
			return err
		}
		rc, err := zf.Open()
		if err != nil {
			return err
		}
		out, err := os.OpenFile(dest, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0644)
		if err != nil {
			rc.Close()
			return err
		}
		_, cpErr := io.Copy(out, rc)
		rc.Close()
		closeErr := out.Close()
		if cpErr != nil {
			return cpErr
		}
		if closeErr != nil {
			return closeErr
		}
	}
	return nil
}

func writeLauncher(installDir, appDir string) error {
	bat := "@echo off\r\n" +
		"chcp 65001 >nul\r\n" +
		"cd /d \"%~dp0app\"\r\n" +
		"start \"\" \"runtime\\pythonw.exe\" \"总控台.py\"\r\n"
	return os.WriteFile(filepath.Join(installDir, "启动智能考场.bat"), []byte(bat), 0644)
}

func psQuote(s string) string {
	return "'" + strings.ReplaceAll(s, "'", "''") + "'"
}

func runPowerShell(script string) error {
	// UTF-16LE + Base64（-EncodedCommand），避免中文路径/引号在 -Command 下被转义破坏。
	u := utf16.Encode([]rune(script))
	buf := make([]byte, len(u)*2)
	for i, r := range u {
		binary.LittleEndian.PutUint16(buf[i*2:], r)
	}
	enc := base64.StdEncoding.EncodeToString(buf)
	cmd := exec.Command("powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc)
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("powershell: %v %s", err, strings.TrimSpace(string(out)))
	}
	return nil
}

func runPowerShellFile(scriptPath string) error {
	cmd := exec.Command("powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", scriptPath)
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("powershell: %v %s", err, strings.TrimSpace(string(out)))
	}
	return nil
}

func writeUTF16LE(path, text string) error {
	u := utf16.Encode([]rune(text))
	buf := make([]byte, 2+len(u)*2)
	buf[0], buf[1] = 0xFF, 0xFE
	for i, r := range u {
		binary.LittleEndian.PutUint16(buf[2+i*2:], r)
	}
	return os.WriteFile(path, buf, 0600)
}

func desktopDir() string {
	proc := shell32.NewProc("SHGetFolderPathW")
	var buf [260]uint16
	if r, _, _ := proc.Call(0, 0x10, 0, 0, uintptr(unsafe.Pointer(&buf[0]))); r == 0 {
		if s := syscall.UTF16ToString(buf[:]); s != "" {
			return s
		}
	}
	return filepath.Join(os.Getenv("USERPROFILE"), "Desktop")
}

func createShortcut(installDir, appDir string) error {
	pyw := filepath.Join(appDir, "runtime", "pythonw.exe")
	entry := filepath.Join(appDir, "总控台.py")
	name := "启动智能考场.lnk"
	localLnk := filepath.Join(installDir, name)
	script := fmt.Sprintf(
		"$ErrorActionPreference='Stop'\r\n"+
			"$s=(New-Object -ComObject WScript.Shell).CreateShortcut(%s)\r\n"+
			"$s.TargetPath=%s\r\n$s.Arguments=%s\r\n$s.WorkingDirectory=%s\r\n"+
			"$s.Description='智能考场 AI 巡考系统'\r\n$s.Save()\r\n"+
			"if (!(Test-Path -LiteralPath %s)) { throw 'shortcut not created' }\r\n",
		psQuote(localLnk), psQuote(pyw), psQuote("\""+entry+"\""), psQuote(appDir), psQuote(localLnk))
	ps1 := filepath.Join(installDir, ".make_shortcut.ps1")
	if err := writeUTF16LE(ps1, script); err != nil {
		return err
	}
	err := runPowerShellFile(ps1)
	os.Remove(ps1)
	return err
}
func install(exePath string, env *envelope, installDir string) error {
	appDir := filepath.Join(installDir, "app")
	if err := os.MkdirAll(appDir, 0755); err != nil {
		return fmt.Errorf("无法创建安装目录：%w", err)
	}
	tmp := filepath.Join(installDir, ".zk_payload.zip")
	_ = os.Remove(tmp)
	defer os.Remove(tmp)
	if err := decryptToFile(exePath, env, tmp); err != nil {
		return err
	}
	fmt.Print("正在解压程序文件 ... ")
	if err := extractZip(tmp, appDir); err != nil {
		return err
	}
	fmt.Println("\r正在解压程序文件 ... 100%")
	if err := writeLauncher(installDir, appDir); err != nil {
		return err
	}
	if err := createShortcut(installDir, appDir); err != nil {
		fmt.Println("（提示）桌面快捷方式创建失败，不影响使用：", err)
	}
	return nil
}

func main() {
	setConsole()
	if r, _, _ := procIsDebuggerPresent.Call(); r != 0 {
		msgBox(appTitle+" · 安装失败", "检测到调试器，安装程序已退出。", 0x10)
		return
	}
	// 自动化测试/静默部署参数（普通用户双击时不传参数，仍走图形选择路径）。
	var targetDir string
	assumeYes, noLaunch, silent := false, false, false
	args := os.Args[1:]
	for i := 0; i < len(args); i++ {
		switch args[i] {
		case "--dir":
			if i+1 < len(args) {
				targetDir = args[i+1]
				i++
			}
		case "--yes":
			assumeYes = true
		case "--no-launch":
			noLaunch = true
		case "--silent":
			silent = true
		}
	}
	exePath, err := os.Executable()
	if err != nil {
		fatal(appTitle+" · 安装失败", "无法定位安装包："+err.Error())
	}
	env, err := readEnvelope(exePath)
	if err != nil {
		fatal(appTitle+" · 安装失败", err.Error())
	}
	localApp := os.Getenv("LOCALAPPDATA")
	defaultDir := filepath.Join(localApp, "Programs", "智能考场AI巡考系统")
	if localApp == "" {
		defaultDir = "C:\\智能考场AI巡考系统"
	}
	var dir string
	if targetDir != "" {
		dir = targetDir
	} else {
		fmt.Println("============================================================")
		fmt.Println("  " + appTitle + " · 安装程序")
		fmt.Println("  版权 © 2026 EDY_POWERQ · 公司：EDY_POWERQ")
		fmt.Println("============================================================")
		fmt.Println("请选择安装位置（建议安装到非系统盘或当前用户目录）。")
		var ok bool
		dir, ok = browseFolder("选择 "+appTitle+" 的安装目录", defaultDir)
		if !ok {
			fmt.Println("已取消安装。")
			return
		}
	}
	dir = filepath.Clean(dir)
	if dir == "." || dir == "" {
		dir = defaultDir
	}
	if !assumeYes {
		if r := msgBox(appTitle+" · 确认安装", "将安装到：\r\n"+dir+"\r\n\r\n继续安装？", 0x04|0x40); r != 6 {
			fmt.Println("已取消安装。")
			return
		}
	}
	fmt.Println("安装到：", dir)
	if err := install(exePath, env, dir); err != nil {
		fatal(appTitle+" · 安装失败", err.Error()+"\r\n\r\n请换一个可写目录后重试。")
	}
	appDir := filepath.Join(dir, "app")
	pyw := filepath.Join(appDir, "runtime", "pythonw.exe")
	if _, err := os.Stat(pyw); err != nil {
		fatal(appTitle+" · 安装失败", "安装完成但找不到 runtime\\pythonw.exe，请检查安装包是否完整。")
	}
	if !silent {
		msgBox(appTitle+" · 安装完成", "安装完成！\r\n\r\n安装目录：\r\n"+dir+
			"\r\n\r\n已生成“启动智能考场.lnk”和“启动智能考场.bat”。"+
			"\r\n版权 © 2026 EDY_POWERQ · EDY_POWERQ\r\n点击“确定”后启动总控台。", 0x40)
	}
	if noLaunch {
		fmt.Println("安装完成（未启动）：", dir)
		return
	}
	cmd := exec.Command(pyw, filepath.Join(appDir, "总控台.py"))
	if err := cmd.Start(); err != nil {
		if !silent {
			msgBox(appTitle+" · 启动失败", "安装已完成，但启动失败：\r\n"+err.Error()+
				"\r\n\r\n请双击安装目录下的“启动智能考场.bat”。", 0x10)
		}
	}
}
