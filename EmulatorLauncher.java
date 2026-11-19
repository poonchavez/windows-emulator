import java.awt.BorderLayout;
import java.awt.Dimension;
import java.awt.FlowLayout;
import java.awt.Font;
import java.awt.GridLayout;
import java.io.File;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import javax.swing.BorderFactory;
import javax.swing.JButton;
import javax.swing.JComboBox;
import javax.swing.JFileChooser;
import javax.swing.JFrame;
import javax.swing.JLabel;
import javax.swing.JOptionPane;
import javax.swing.JPanel;
import javax.swing.JTextField;
import javax.swing.SwingUtilities;
import javax.swing.UIManager;
import javax.swing.filechooser.FileNameExtensionFilter;

/** Boots real Windows installations in QEMU; Windows media is not included. */
public final class EmulatorLauncher {
    private static final String[] VERSIONS = {
        "Windows XP", "Windows Vista", "Windows 7", "Windows 8", "Windows 8.1"
    };
    private static final Path VM_HOME = Paths.get(System.getProperty("user.home"), "Windows-Emulator", "uefi-tpm2");
    private final JFrame frame = new JFrame("Windows Emulator — Virtual Machine Launcher");
    private final JComboBox<String> version = new JComboBox<>(VERSIONS);
    private final JTextField isoPath = new JTextField();
    private final JLabel status = new JLabel("Choose a Windows version, then install from your own ISO or start an installed VM.");

    public static void main(String[] args) {
        SwingUtilities.invokeLater(() -> new EmulatorLauncher().show());
    }

    private void show() {
        try { UIManager.setLookAndFeel(UIManager.getSystemLookAndFeelClassName()); }
        catch (Exception ignored) { }

        frame.setDefaultCloseOperation(JFrame.EXIT_ON_CLOSE);
        frame.setMinimumSize(new Dimension(680, 390));
        frame.setSize(760, 450);
        frame.setLocationRelativeTo(null);

        JPanel page = new JPanel(new BorderLayout(0, 20));
        page.setBorder(BorderFactory.createEmptyBorder(28, 32, 24, 32));
        JPanel heading = new JPanel(new GridLayout(0, 1, 0, 8));
        JLabel title = new JLabel("Run the real Windows desktop");
        title.setFont(title.getFont().deriveFont(Font.BOLD, 24f));
        heading.add(title);
        heading.add(new JLabel("QEMU virtual machines boot Windows itself, including its original interface and apps."));
        heading.add(new JLabel("Vista and newer use UEFI; all VMs get a persistent TPM 2.0 device."));

        JPanel controls = new JPanel(new GridLayout(0, 1, 0, 10));
        controls.add(new JLabel("Windows version"));
        controls.add(version);
        controls.add(new JLabel("Installation ISO (needed only for a new installation)"));
        JPanel isoRow = new JPanel(new BorderLayout(8, 0));
        isoRow.add(isoPath, BorderLayout.CENTER);
        JButton browse = new JButton("Choose ISO…");
        browse.addActionListener(e -> chooseIso());
        isoRow.add(browse, BorderLayout.EAST);
        controls.add(isoRow);

        JPanel actions = new JPanel(new FlowLayout(FlowLayout.LEFT, 10, 0));
        JButton install = new JButton("Create VM and install");
        install.addActionListener(e -> launch(true));
        JButton start = new JButton("Start installed VM");
        start.addActionListener(e -> launch(false));
        actions.add(install);
        actions.add(start);

        JPanel lower = new JPanel(new BorderLayout(0, 14));
        lower.add(actions, BorderLayout.NORTH);
        status.setBorder(BorderFactory.createEmptyBorder(10, 0, 0, 0));
        lower.add(status, BorderLayout.CENTER);

        page.add(heading, BorderLayout.NORTH);
        page.add(controls, BorderLayout.CENTER);
        page.add(lower, BorderLayout.SOUTH);
        frame.setContentPane(page);
        frame.setVisible(true);
    }

    private void chooseIso() {
        JFileChooser chooser = new JFileChooser();
        chooser.setDialogTitle("Select a Windows installation ISO");
        chooser.setFileFilter(new FileNameExtensionFilter("ISO disc image (*.iso)", "iso"));
        if (chooser.showOpenDialog(frame) == JFileChooser.APPROVE_OPTION) {
            isoPath.setText(chooser.getSelectedFile().getAbsolutePath());
        }
    }

    private void launch(boolean install) {
        String os = (String) version.getSelectedItem();
        String key = os.toLowerCase().replace(' ', '-').replace('.', '-');
        Path vmDir = VM_HOME.resolve(key);
        Path disk = vmDir.resolve("disk.qcow2");
        TpmSession tpm = null;
        try {
            String qemu = findExecutable("qemu-system-x86_64");
            if (qemu == null) throw new IOException("QEMU was not found. Install QEMU and make qemu-system-x86_64 available on PATH.");
            Files.createDirectories(vmDir);

            List<String> command = new ArrayList<>(Arrays.asList(qemu,
                "-machine", machineForHost(), "-m", memoryFor(os), "-smp", coresFor(os), "-cpu", "qemu64",
                "-vga", "std", "-device", "usb-tablet",
                "-netdev", "user,id=net0", "-device", "rtl8139,netdev=net0",
                "-drive", "file=" + disk + ",format=qcow2,if=ide"));

            if (!os.contains("XP")) addUefi(command, vmDir, os);

            if (install) {
                String selectedIso = isoPath.getText().trim();
                if (selectedIso.isEmpty() || !Files.isRegularFile(Paths.get(selectedIso))) {
                    throw new IOException("Choose a valid Windows ISO first.");
                }
                if (!Files.exists(disk)) {
                    String qemuImg = findExecutable("qemu-img");
                    if (qemuImg == null) throw new IOException("qemu-img was not found. Install the complete QEMU package.");
                    runAndCheck(Arrays.asList(qemuImg, "create", "-f", "qcow2", disk.toString(), diskSizeFor(os)));
                }
                command.addAll(Arrays.asList("-cdrom", selectedIso, "-boot", "order=dc"));
            } else {
                if (!Files.isRegularFile(disk)) {
                    throw new IOException("No VM disk exists yet. Choose an ISO and select Create VM and install.");
                }
            }

            tpm = startTpm(vmDir);
            command.addAll(Arrays.asList("-chardev", "socket,id=chrtpm,path=" + tpm.socket,
                "-tpmdev", "emulator,id=tpm0,chardev=chrtpm", "-device", "tpm-tis,tpmdev=tpm0"));
            status.setText("Starting " + os + " in QEMU… VM files: " + vmDir);
            Process vm = new ProcessBuilder(command).inheritIO().start();
            TpmSession activeTpm = tpm;
            vm.onExit().thenRun(activeTpm::stop);
        } catch (Exception ex) {
            if (tpm != null) tpm.stop();
            status.setText("Could not start the virtual machine.");
            JOptionPane.showMessageDialog(frame, ex.getMessage(), "Windows Emulator", JOptionPane.ERROR_MESSAGE);
        }
    }

    private static void addUefi(List<String> command, Path vmDir, String os) throws IOException {
        FirmwarePair firmware = findFirmware(false);
        if (firmware == null) {
            throw new IOException("UEFI firmware was not found. Install QEMU's OVMF/EDK II firmware package, then restart the launcher.");
        }
        Path variableStore = vmDir.resolve("uefi-vars.fd");
        if (!Files.exists(variableStore)) {
            Files.copy(firmware.variables, variableStore);
        }
        command.addAll(Arrays.asList("-drive", "if=pflash,format=raw,readonly=on,file=" + firmware.code,
            "-drive", "if=pflash,format=raw,file=" + variableStore));
    }

    private static FirmwarePair findFirmware(boolean secureBoot) {
        List<Path[]> candidates = new ArrayList<>();
        String[] roots = {
            "/opt/homebrew/share/qemu", "/usr/local/share/qemu", "/opt/local/share/qemu",
            "/usr/share/OVMF", "/usr/share/ovmf", "/usr/share/edk2/ovmf",
            "/usr/share/edk2/x64", "/usr/share/edk2-ovmf/x64", "/usr/share/qemu"
        };
        for (String root : roots) {
            Path dir = Paths.get(root);
            if (secureBoot) {
                candidates.add(new Path[]{dir.resolve("OVMF_CODE_4M.secboot.fd"), dir.resolve("OVMF_VARS_4M.ms.fd")});
                candidates.add(new Path[]{dir.resolve("OVMF_CODE_4M.secboot.fd"), dir.resolve("OVMF_VARS_4M.fd")});
                candidates.add(new Path[]{dir.resolve("OVMF_CODE.secboot.fd"), dir.resolve("OVMF_VARS.ms.fd")});
                candidates.add(new Path[]{dir.resolve("OVMF_CODE.secboot.fd"), dir.resolve("OVMF_VARS.secboot.fd")});
                candidates.add(new Path[]{dir.resolve("edk2-x86_64-secure-code.fd"), dir.resolve("edk2-i386-vars.fd")});
            }
            candidates.add(new Path[]{dir.resolve("OVMF_CODE_4M.fd"), dir.resolve("OVMF_VARS_4M.fd")});
            candidates.add(new Path[]{dir.resolve("OVMF_CODE.4m.fd"), dir.resolve("OVMF_VARS.4m.fd")});
            candidates.add(new Path[]{dir.resolve("OVMF_CODE.fd"), dir.resolve("OVMF_VARS.fd")});
            candidates.add(new Path[]{dir.resolve("edk2-x86_64-code.fd"), dir.resolve("edk2-i386-vars.fd")});
        }
        for (Path[] pair : candidates) {
            if (Files.isRegularFile(pair[0]) && Files.isRegularFile(pair[1])) {
                return new FirmwarePair(pair[0], pair[1]);
            }
        }
        return null;
    }

    private static TpmSession startTpm(Path vmDir) throws IOException, InterruptedException {
        if (isWindows()) throw new IOException("TPM 2.0 setup currently needs swtpm with Unix socket support. On Windows, run this launcher through WSL with QEMU and swtpm installed.");
        String swtpm = findExecutable("swtpm");
        if (swtpm == null) throw new IOException("swtpm was not found. Install swtpm (for example: brew install swtpm on macOS, or install the swtpm package on Linux) and restart the launcher.");
        Path state = vmDir.resolve("tpm-state");
        Files.createDirectories(state);
        Path socketDir = Files.createTempDirectory("windows-emulator-tpm-");
        Path socket = socketDir.resolve("swtpm.sock");
        Path log = vmDir.resolve("swtpm.log");
        List<String> args = Arrays.asList(swtpm, "socket", "--tpmstate", "dir=" + state,
            "--ctrl", "type=unixio,path=" + socket, "--tpm2", "--log", "file=" + log + ",level=20");
        Process process = new ProcessBuilder(args).redirectErrorStream(true)
            .redirectOutput(ProcessBuilder.Redirect.appendTo(log.toFile())).start();
        long deadline = System.currentTimeMillis() + 5000;
        while (!Files.exists(socket) && process.isAlive() && System.currentTimeMillis() < deadline) Thread.sleep(100);
        if (!process.isAlive() || !Files.exists(socket)) {
            String detail = process.isAlive() ? "did not create its control socket" : "exited with code " + process.exitValue();
            process.destroyForcibly();
            deleteTree(socketDir);
            throw new IOException("swtpm " + detail + ". See " + log + " for details.");
        }
        return new TpmSession(process, socket, socketDir);
    }

    private static void deleteTree(Path dir) {
        try (java.util.stream.Stream<Path> paths = Files.walk(dir)) {
            paths.sorted(java.util.Comparator.reverseOrder()).forEach(path -> {
                try { Files.deleteIfExists(path); } catch (IOException ignored) { }
            });
        } catch (IOException ignored) { }
    }

    private static final class FirmwarePair {
        final Path code, variables;
        FirmwarePair(Path code, Path variables) { this.code = code; this.variables = variables; }
    }

    private static final class TpmSession {
        final Process process;
        final Path socket, socketDir;
        TpmSession(Process process, Path socket, Path socketDir) {
            this.process = process; this.socket = socket; this.socketDir = socketDir;
        }
        void stop() { process.destroy(); deleteTree(socketDir); }
    }

    private static void runAndCheck(List<String> command) throws IOException, InterruptedException {
        Process process = new ProcessBuilder(command).inheritIO().start();
        int result = process.waitFor();
        if (result != 0) throw new IOException("qemu-img could not create the virtual disk (exit code " + result + ").");
    }

    private static String findExecutable(String name) {
        String path = System.getenv("PATH");
        if (path == null) return null;
        String executable = isWindows() ? name + ".exe" : name;
        for (String dir : path.split(java.util.regex.Pattern.quote(File.pathSeparator))) {
            Path candidate = Paths.get(dir, executable);
            if (Files.isRegularFile(candidate) && (isWindows() || Files.isExecutable(candidate))) return candidate.toString();
        }
        return null;
    }

    private static String memoryFor(String os) {
        if (os.contains("XP")) return "1024";
        if (os.contains("Vista") || os.contains("7") || os.contains("8")) return "2048";
        return "4096";
    }

    private static String coresFor(String os) { return os.contains("XP") ? "1" : "2"; }
    private static String diskSizeFor(String os) { return os.contains("XP") ? "20G" : os.contains("Vista") || os.contains("7") ? "40G" : "64G"; }
    private static boolean isWindows() { return System.getProperty("os.name", "").toLowerCase().contains("win"); }
    private static String machineForHost() {
        String host = System.getProperty("os.name", "").toLowerCase();
        if (host.contains("mac") || host.contains("darwin")) return "pc,accel=hvf";
        if (host.contains("linux")) return "pc,accel=kvm";
        if (host.contains("win")) return "pc,accel=whpx";
        return "pc";
    }
}
