using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.IO;
using System.Net;
using System.Text;
using System.Windows.Forms;

namespace BatchVoiceDesktop
{
    internal static class Theme
    {
        public static readonly Color Background = Color.FromArgb(246, 248, 251);
        public static readonly Color Surface = Color.White;
        public static readonly Color Border = Color.FromArgb(226, 232, 240);
        public static readonly Color Text = Color.FromArgb(15, 23, 42);
        public static readonly Color Muted = Color.FromArgb(100, 116, 139);
        public static readonly Color Blue = Color.FromArgb(37, 99, 235);
        public static readonly Color Green = Color.FromArgb(5, 150, 105);
        public static readonly Color Red = Color.FromArgb(220, 38, 38);
        public static readonly Color Dark = Color.FromArgb(15, 23, 42);
    }

    internal static class Drawing
    {
        public static GraphicsPath Rounded(Rectangle bounds, int radius)
        {
            int d = radius * 2;
            var path = new GraphicsPath();
            path.AddArc(bounds.X, bounds.Y, d, d, 180, 90);
            path.AddArc(bounds.Right - d, bounds.Y, d, d, 270, 90);
            path.AddArc(bounds.Right - d, bounds.Bottom - d, d, d, 0, 90);
            path.AddArc(bounds.X, bounds.Bottom - d, d, d, 90, 90);
            path.CloseFigure();
            return path;
        }

        public static Color Blend(Color a, Color b, float amount)
        {
            return Color.FromArgb(
                (int)(a.R + (b.R - a.R) * amount),
                (int)(a.G + (b.G - a.G) * amount),
                (int)(a.B + (b.B - a.B) * amount));
        }
    }

    internal sealed class CardPanel : Panel
    {
        public int Radius { get; set; }
        public Color BorderColor { get; set; }

        public CardPanel()
        {
            Radius = 12;
            BorderColor = Theme.Border;
            BackColor = Color.Transparent;
            DoubleBuffered = true;
            Padding = new Padding(1);
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            Rectangle rect = new Rectangle(0, 0, Width - 1, Height - 1);
            using (GraphicsPath path = Drawing.Rounded(rect, Radius))
            using (SolidBrush brush = new SolidBrush(Theme.Surface))
            using (Pen pen = new Pen(BorderColor)) {
                e.Graphics.FillPath(brush, path);
                e.Graphics.DrawPath(pen, path);
                Region = new Region(path);
            }
            base.OnPaint(e);
        }
    }

    internal sealed class ModernButton : Control
    {
        private readonly Timer animation;
        private float hover;
        private bool hoverTarget;

        public Color FillColor { get; set; }
        public Color TextColor { get; set; }
        public Color OutlineColor { get; set; }
        public string IconGlyph { get; set; }
        public string Subtitle { get; set; }
        public bool IsOutline { get; set; }
        public bool IsActive { get; set; }
        public int Radius { get; set; }

        public ModernButton()
        {
            FillColor = Theme.Surface;
            TextColor = Theme.Text;
            OutlineColor = Theme.Border;
            IconGlyph = "";
            Subtitle = "";
            Radius = 9;
            Cursor = Cursors.Hand;
            DoubleBuffered = true;
            TabStop = true;
            Font = new Font("Microsoft YaHei UI", 9.5F, FontStyle.Bold);
            animation = new Timer { Interval = 16 };
            animation.Tick += delegate {
                float target = hoverTarget ? 1F : 0F;
                hover += (target - hover) * 0.24F;
                if (Math.Abs(target - hover) < 0.02F) { hover = target; animation.Stop(); }
                Invalidate();
            };
        }

        protected override void OnMouseEnter(EventArgs e) { hoverTarget = true; animation.Start(); base.OnMouseEnter(e); }
        protected override void OnMouseLeave(EventArgs e) { hoverTarget = false; animation.Start(); base.OnMouseLeave(e); }
        protected override void OnMouseDown(MouseEventArgs e) { if (e.Button == MouseButtons.Left) { Capture = true; Invalidate(); } base.OnMouseDown(e); }
        protected override void OnMouseUp(MouseEventArgs e) { Capture = false; Invalidate(); base.OnMouseUp(e); }

        protected override void OnPaint(PaintEventArgs e)
        {
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            Rectangle rect = new Rectangle(0, 0, Width - 1, Height - 1);
            Color baseFill = IsActive ? Color.FromArgb(235, 243, 255) : FillColor;
            Color fill = Drawing.Blend(baseFill, IsOutline ? Color.FromArgb(241, 245, 249) : Color.White, hover * 0.18F);
            if (!Enabled) { fill = Color.FromArgb(241, 245, 249); }

            using (GraphicsPath path = Drawing.Rounded(rect, Radius))
            using (SolidBrush brush = new SolidBrush(fill))
            using (Pen pen = new Pen(IsActive ? Color.FromArgb(191, 219, 254) : OutlineColor)) {
                e.Graphics.FillPath(brush, path);
                if (IsOutline || IsActive) e.Graphics.DrawPath(pen, path);
            }

            Color fore = Enabled ? (IsActive ? Theme.Blue : TextColor) : Color.FromArgb(148, 163, 184);
            int iconWidth = String.IsNullOrEmpty(IconGlyph) ? 0 : 34;
            int contentWidth = iconWidth + TextRenderer.MeasureText(Text, Font).Width;
            int startX = Math.Max(14, (Width - contentWidth) / 2);

            if (!String.IsNullOrEmpty(IconGlyph)) {
                using (var iconFont = new Font("Segoe MDL2 Assets", Subtitle.Length > 0 ? 17F : 13F))
                using (var iconBrush = new SolidBrush(fore)) {
                    e.Graphics.DrawString(IconGlyph, iconFont, iconBrush, startX, Subtitle.Length > 0 ? 18 : (Height - 22) / 2);
                }
            }

            if (Subtitle.Length > 0) {
                TextRenderer.DrawText(e.Graphics, Text, new Font(Font.FontFamily, 11F, FontStyle.Bold), new Point(startX + iconWidth, 14), fore);
                TextRenderer.DrawText(e.Graphics, Subtitle, new Font(Font.FontFamily, 8.5F), new Point(startX + iconWidth, 40), Drawing.Blend(fore, Color.White, 0.18F));
            } else {
                TextRenderer.DrawText(e.Graphics, Text, Font, new Rectangle(startX + iconWidth, 0, Width - startX - iconWidth - 8, Height), fore, TextFormatFlags.VerticalCenter | TextFormatFlags.Left | TextFormatFlags.EndEllipsis);
            }
        }
    }

    internal sealed class ProgressRing : Control
    {
        private int percentage;
        public int Percentage { get { return percentage; } set { percentage = Math.Max(0, Math.Min(100, value)); Invalidate(); } }

        public ProgressRing()
        {
            Size = new Size(92, 92);
            DoubleBuffered = true;
            BackColor = Color.White;
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            Rectangle ring = new Rectangle(9, 9, Width - 18, Height - 18);
            using (var pen = new Pen(Color.FromArgb(226, 232, 240), 8F)) e.Graphics.DrawArc(pen, ring, -90, 360);
            if (percentage > 0) using (var pen = new Pen(Theme.Blue, 8F)) { pen.StartCap = LineCap.Round; pen.EndCap = LineCap.Round; e.Graphics.DrawArc(pen, ring, -90, percentage * 3.6F); }
            using (var valueFont = new Font("Microsoft YaHei UI", 14F, FontStyle.Bold))
            using (var valueBrush = new SolidBrush(Theme.Text)) {
                string text = percentage + "%";
                SizeF size = e.Graphics.MeasureString(text, valueFont);
                e.Graphics.DrawString(text, valueFont, valueBrush, (Width - size.Width) / 2, 27);
            }
            using (var labelFont = new Font("Microsoft YaHei UI", 8F))
            using (var labelBrush = new SolidBrush(Theme.Muted)) {
                string text = "已处理";
                SizeF size = e.Graphics.MeasureString(text, labelFont);
                e.Graphics.DrawString(text, labelFont, labelBrush, (Width - size.Width) / 2, 53);
            }
        }
    }

    internal sealed class CatalogDialog : Form
    {
        private readonly DataGridView categoryGrid;
        private readonly DataGridView languageGrid;
        public List<string> Projects { get; private set; }
        public List<string> Languages { get; private set; }

        public CatalogDialog(List<string> projects, List<string> languages)
        {
            Text = "项目与语言管理";
            StartPosition = FormStartPosition.CenterParent;
            Size = new Size(900, 620);
            MinimumSize = new Size(760, 540);
            BackColor = Theme.Background;
            Font = new Font("Microsoft YaHei UI", 9F);

            var title = MainForm.CreateLabel("项目与语言管理", 18F, FontStyle.Bold, Theme.Text, 28, 20, 300, 38);
            var subtitle = MainForm.CreateLabel("项目下面直接管理不同语言的文案任务", 9F, FontStyle.Regular, Theme.Muted, 30, 58, 500, 24);
            Controls.Add(title); Controls.Add(subtitle);

            var categoryCard = new CardPanel { Location = new Point(24, 96), Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right, Size = new Size(548, 438) };
            categoryCard.Controls.Add(MainForm.CreateLabel("项目列表", 11F, FontStyle.Bold, Theme.Text, 18, 14, 240, 28));
            categoryGrid = MakeGrid();
            categoryGrid.Location = new Point(18, 52);
            categoryGrid.Size = new Size(510, 320);
            categoryGrid.Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right;
            categoryGrid.Columns.Add(new DataGridViewTextBoxColumn { Name = "project", HeaderText = "项目名称", AutoSizeMode = DataGridViewAutoSizeColumnMode.Fill });
            foreach (string project in projects) categoryGrid.Rows.Add(project);
            var addCategory = SmallButton("添加项目", "\uE710", Theme.Blue, Color.White, false);
            var removeCategory = SmallButton("删除选中", "\uE74D", Theme.Red, Theme.Red, true);
            addCategory.Location = new Point(18, 385); removeCategory.Location = new Point(142, 385);
            addCategory.Anchor = AnchorStyles.Bottom | AnchorStyles.Left; removeCategory.Anchor = AnchorStyles.Bottom | AnchorStyles.Left;
            addCategory.Click += delegate { int row = categoryGrid.Rows.Add(""); categoryGrid.CurrentCell = categoryGrid.Rows[row].Cells[0]; categoryGrid.BeginEdit(true); };
            removeCategory.Click += delegate { RemoveRows(categoryGrid); };
            categoryCard.Controls.Add(categoryGrid); categoryCard.Controls.Add(addCategory); categoryCard.Controls.Add(removeCategory);

            var languageCard = new CardPanel { Location = new Point(588, 96), Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Right, Size = new Size(286, 438) };
            languageCard.Controls.Add(MainForm.CreateLabel("语言选项", 11F, FontStyle.Bold, Theme.Text, 18, 14, 180, 28));
            languageGrid = MakeGrid();
            languageGrid.Location = new Point(18, 52);
            languageGrid.Size = new Size(250, 320);
            languageGrid.Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right;
            languageGrid.Columns.Add(new DataGridViewTextBoxColumn { Name = "name", HeaderText = "语言名称", AutoSizeMode = DataGridViewAutoSizeColumnMode.Fill });
            foreach (string language in languages) languageGrid.Rows.Add(language);
            var addLanguage = SmallButton("添加语言", "\uE710", Theme.Blue, Color.White, false);
            var removeLanguage = SmallButton("删除选中", "\uE74D", Theme.Red, Theme.Red, true);
            addLanguage.Location = new Point(18, 385); removeLanguage.Location = new Point(142, 385);
            addLanguage.Anchor = AnchorStyles.Bottom | AnchorStyles.Left; removeLanguage.Anchor = AnchorStyles.Bottom | AnchorStyles.Left;
            addLanguage.Click += delegate { int row = languageGrid.Rows.Add(""); languageGrid.CurrentCell = languageGrid.Rows[row].Cells[0]; languageGrid.BeginEdit(true); };
            removeLanguage.Click += delegate { RemoveRows(languageGrid); };
            languageCard.Controls.Add(languageGrid); languageCard.Controls.Add(addLanguage); languageCard.Controls.Add(removeLanguage);

            var cancel = new ModernButton { Text = "取消", IsOutline = true, FillColor = Color.White, Location = new Point(630, 548), Size = new Size(110, 40), Anchor = AnchorStyles.Bottom | AnchorStyles.Right };
            var save = new ModernButton { Text = "保存设置", IconGlyph = "\uE74E", FillColor = Theme.Green, TextColor = Color.White, Location = new Point(752, 548), Size = new Size(122, 40), Anchor = AnchorStyles.Bottom | AnchorStyles.Right };
            cancel.Click += delegate { DialogResult = DialogResult.Cancel; Close(); };
            save.Click += delegate { SaveAndClose(); };
            Controls.Add(categoryCard); Controls.Add(languageCard); Controls.Add(cancel); Controls.Add(save);
        }

        private static DataGridView MakeGrid()
        {
            var grid = new DataGridView { BackgroundColor = Color.White, BorderStyle = BorderStyle.None, CellBorderStyle = DataGridViewCellBorderStyle.SingleHorizontal, GridColor = Theme.Border, EnableHeadersVisualStyles = false, ColumnHeadersBorderStyle = DataGridViewHeaderBorderStyle.None, ColumnHeadersHeight = 38, RowHeadersVisible = false, RowTemplate = { Height = 38 }, AllowUserToAddRows = false, AllowUserToDeleteRows = false, AllowUserToResizeRows = false, SelectionMode = DataGridViewSelectionMode.FullRowSelect, MultiSelect = true, EditMode = DataGridViewEditMode.EditOnKeystrokeOrF2 };
            grid.ColumnHeadersDefaultCellStyle = new DataGridViewCellStyle { BackColor = Color.FromArgb(248, 250, 252), ForeColor = Theme.Muted, Font = new Font("Microsoft YaHei UI", 9F, FontStyle.Bold), Padding = new Padding(8, 0, 8, 0) };
            grid.DefaultCellStyle = new DataGridViewCellStyle { BackColor = Color.White, ForeColor = Theme.Text, SelectionBackColor = Color.FromArgb(239, 246, 255), SelectionForeColor = Theme.Text, Padding = new Padding(8, 4, 8, 4) };
            return grid;
        }

        private static ModernButton SmallButton(string text, string icon, Color color, Color textColor, bool outline)
        {
            return new ModernButton { Text = text, IconGlyph = icon, IsOutline = outline, OutlineColor = outline ? Color.FromArgb(254, 202, 202) : color, FillColor = outline ? Color.White : color, TextColor = textColor, Size = new Size(114, 38) };
        }

        private static void RemoveRows(DataGridView grid)
        {
            var rows = new List<int>();
            foreach (DataGridViewRow row in grid.SelectedRows) rows.Add(row.Index);
            rows.Sort(); rows.Reverse();
            foreach (int index in rows) if (index >= 0 && index < grid.Rows.Count) grid.Rows.RemoveAt(index);
        }

        private void SaveAndClose()
        {
            categoryGrid.EndEdit(); languageGrid.EndEdit();
            Projects = new List<string>();
            foreach (DataGridViewRow row in categoryGrid.Rows) {
                string project = Convert.ToString(row.Cells["project"].Value).Trim();
                if (project.Length > 0 && !Projects.Contains(project)) Projects.Add(project);
            }
            Languages = new List<string>();
            foreach (DataGridViewRow row in languageGrid.Rows) {
                string name = Convert.ToString(row.Cells["name"].Value).Trim();
                if (name.Length > 0 && !Languages.Contains(name)) Languages.Add(name);
            }
            DialogResult = DialogResult.OK;
            Close();
        }
    }

    internal sealed class OutputDirectoryRule
    {
        public string Project { get; set; }
        public string Material { get; set; }
        public string BaseDir { get; set; }
        public string FolderName { get; set; }
        public string DateMode { get; set; }
        public string DateValue { get; set; }
        public string LegacyOutputDir { get; set; }
    }

    internal sealed class OutputDirectoryDialog : Form
    {
        private readonly DataGridView grid;
        private readonly RadioButton autoDate;
        private readonly RadioButton manualDate;
        private readonly RadioButton noDate;
        private readonly DateTimePicker datePicker;
        private readonly Label preview;
        public List<OutputDirectoryRule> Rules { get; private set; }

        public OutputDirectoryDialog(List<OutputDirectoryRule> rules)
        {
            Text = "项目输出目录设置";
            StartPosition = FormStartPosition.CenterParent;
            Size = new Size(940, 610);
            MinimumSize = new Size(820, 560);
            BackColor = Theme.Background;
            Font = new Font("Microsoft YaHei UI", 9F);

            Controls.Add(MainForm.CreateLabel("项目输出目录", 18F, FontStyle.Bold, Theme.Text, 28, 20, 300, 38));
            Controls.Add(MainForm.CreateLabel("选择上级位置、设置目录名称，并自动按当天日期建立子文件夹", 9F, FontStyle.Regular, Theme.Muted, 30, 58, 650, 24));

            var card = new CardPanel { Location = new Point(24, 94), Size = new Size(876, 322), Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right };
            grid = new DataGridView {
                Location = new Point(16, 16), Size = new Size(842, 288), Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right,
                BackgroundColor = Color.White, BorderStyle = BorderStyle.None, CellBorderStyle = DataGridViewCellBorderStyle.SingleHorizontal,
                GridColor = Theme.Border, EnableHeadersVisualStyles = false, ColumnHeadersBorderStyle = DataGridViewHeaderBorderStyle.None,
                ColumnHeadersHeight = 40, RowHeadersVisible = false, RowTemplate = { Height = 42 }, AllowUserToAddRows = false,
                AllowUserToDeleteRows = false, AllowUserToResizeRows = false, SelectionMode = DataGridViewSelectionMode.FullRowSelect,
                MultiSelect = false, EditMode = DataGridViewEditMode.EditOnKeystrokeOrF2
            };
            grid.ColumnHeadersDefaultCellStyle = new DataGridViewCellStyle { BackColor = Color.FromArgb(248, 250, 252), ForeColor = Theme.Muted, Font = new Font("Microsoft YaHei UI", 9F, FontStyle.Bold), Padding = new Padding(8, 0, 8, 0) };
            grid.DefaultCellStyle = new DataGridViewCellStyle { BackColor = Color.White, ForeColor = Theme.Text, SelectionBackColor = Color.FromArgb(239, 246, 255), SelectionForeColor = Theme.Text, Padding = new Padding(8, 4, 8, 4) };
            grid.Columns.Add(new DataGridViewTextBoxColumn { Name = "project", HeaderText = "项目", Width = 120, ReadOnly = true });
            grid.Columns.Add(new DataGridViewTextBoxColumn { Name = "baseDir", HeaderText = "上级保存位置", AutoSizeMode = DataGridViewAutoSizeColumnMode.Fill });
            grid.Columns.Add(new DataGridViewButtonColumn { Name = "browse", HeaderText = "", Text = "选择…", UseColumnTextForButtonValue = true, Width = 78, FlatStyle = FlatStyle.Flat });
            grid.Columns.Add(new DataGridViewTextBoxColumn { Name = "folderName", HeaderText = "目录名称", Width = 180 });
            foreach (OutputDirectoryRule rule in rules) grid.Rows.Add(rule.Project, rule.BaseDir, "选择…", rule.FolderName);
            grid.CellContentClick += BrowseDirectory;
            grid.CellValueChanged += delegate { UpdatePreview(); };
            grid.SelectionChanged += delegate { UpdatePreview(); };
            card.Controls.Add(grid);

            var dateCard = new CardPanel { Location = new Point(24, 430), Size = new Size(876, 92), Anchor = AnchorStyles.Left | AnchorStyles.Right | AnchorStyles.Bottom };
            dateCard.Controls.Add(MainForm.CreateLabel("日期文件夹", 10F, FontStyle.Bold, Theme.Text, 18, 12, 110, 26));
            autoDate = new RadioButton { Text = "自动使用今天", Location = new Point(138, 14), Size = new Size(130, 24), ForeColor = Theme.Text };
            manualDate = new RadioButton { Text = "手动指定", Location = new Point(282, 14), Size = new Size(102, 24), ForeColor = Theme.Text };
            noDate = new RadioButton { Text = "不建立日期目录", Location = new Point(528, 14), Size = new Size(150, 24), ForeColor = Theme.Text };
            datePicker = new DateTimePicker { Location = new Point(386, 11), Size = new Size(126, 28), Format = DateTimePickerFormat.Custom, CustomFormat = "M.d" };
            preview = MainForm.CreateLabel("", 8.5F, FontStyle.Regular, Theme.Blue, 18, 52, 820, 24);
            dateCard.Controls.Add(autoDate); dateCard.Controls.Add(manualDate); dateCard.Controls.Add(noDate); dateCard.Controls.Add(datePicker); dateCard.Controls.Add(preview);

            string initialMode = rules.Count > 0 ? rules[0].DateMode : "auto";
            if (initialMode == "manual") manualDate.Checked = true;
            else if (initialMode == "none") noDate.Checked = true;
            else autoDate.Checked = true;
            if (rules.Count > 0 && !String.IsNullOrWhiteSpace(rules[0].DateValue)) {
                DateTime parsed;
                if (DateTime.TryParse(DateTime.Now.Year + "." + rules[0].DateValue, out parsed)) datePicker.Value = parsed;
            }
            autoDate.CheckedChanged += delegate { UpdateDateControls(); };
            manualDate.CheckedChanged += delegate { UpdateDateControls(); };
            noDate.CheckedChanged += delegate { UpdateDateControls(); };
            datePicker.ValueChanged += delegate { UpdatePreview(); };

            var cancel = new ModernButton { Text = "取消", IsOutline = true, FillColor = Color.White, Location = new Point(654, 538), Size = new Size(110, 40), Anchor = AnchorStyles.Bottom | AnchorStyles.Right };
            var save = new ModernButton { Text = "保存设置", IconGlyph = "\uE74E", FillColor = Theme.Green, TextColor = Color.White, Location = new Point(778, 538), Size = new Size(122, 40), Anchor = AnchorStyles.Bottom | AnchorStyles.Right };
            cancel.Click += delegate { DialogResult = DialogResult.Cancel; Close(); };
            save.Click += delegate { SaveAndClose(); };
            Controls.Add(card); Controls.Add(dateCard); Controls.Add(cancel); Controls.Add(save);
            UpdateDateControls();
        }

        private void BrowseDirectory(object sender, DataGridViewCellEventArgs e)
        {
            if (e.RowIndex < 0 || grid.Columns[e.ColumnIndex].Name != "browse") return;
            using (var dialog = new FolderBrowserDialog()) {
                dialog.Description = "选择上级保存位置";
                dialog.ShowNewFolderButton = true;
                string current = Convert.ToString(grid.Rows[e.RowIndex].Cells["baseDir"].Value).Trim();
                if (Directory.Exists(current)) dialog.SelectedPath = current;
                if (dialog.ShowDialog(this) == DialogResult.OK) grid.Rows[e.RowIndex].Cells["baseDir"].Value = dialog.SelectedPath;
            }
        }

        private void UpdateDateControls()
        {
            datePicker.Enabled = manualDate.Checked;
            UpdatePreview();
        }

        private void UpdatePreview()
        {
            if (preview == null || grid == null || grid.CurrentRow == null) return;
            string project = Convert.ToString(grid.CurrentRow.Cells["project"].Value);
            string baseDir = Convert.ToString(grid.CurrentRow.Cells["baseDir"].Value).Trim();
            string folder = Convert.ToString(grid.CurrentRow.Cells["folderName"].Value).Trim();
            string date = autoDate.Checked ? DateTime.Now.ToString("M.d") : manualDate.Checked ? datePicker.Value.ToString("M.d") : "";
            string result = baseDir.Length == 0 ? "默认下载目录" : baseDir;
            if (folder.Length > 0) result = Path.Combine(result, folder);
            if (date.Length > 0) result = Path.Combine(result, date);
            preview.Text = project + " 最终目录：" + result;
        }

        private void SaveAndClose()
        {
            grid.EndEdit();
            Rules = new List<OutputDirectoryRule>();
            string mode = autoDate.Checked ? "auto" : manualDate.Checked ? "manual" : "none";
            string value = manualDate.Checked ? datePicker.Value.ToString("M.d") : "";
            foreach (DataGridViewRow row in grid.Rows) {
                string folder = Convert.ToString(row.Cells["folderName"].Value).Trim();
                if (folder.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0) {
                    MessageBox.Show("目录名称不能包含 \\ / : * ? \" < > |", "目录名称无效", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                    return;
                }
                Rules.Add(new OutputDirectoryRule {
                    Project = Convert.ToString(row.Cells["project"].Value).Trim(),
                    Material = "",
                    BaseDir = Convert.ToString(row.Cells["baseDir"].Value).Trim(),
                    FolderName = folder, DateMode = mode, DateValue = value, LegacyOutputDir = ""
                });
            }
            DialogResult = DialogResult.OK;
            Close();
        }
    }

    internal sealed class ProjectRunDialog : Form
    {
        private readonly CheckedListBox projectList;
        public List<string> SelectedProjects { get; private set; }

        public ProjectRunDialog(List<string> projects)
        {
            Text = "选择本次运行项目";
            StartPosition = FormStartPosition.CenterParent;
            Size = new Size(520, 540);
            MinimumSize = new Size(460, 480);
            BackColor = Theme.Background;
            Font = new Font("Microsoft YaHei UI", 9F);

            Controls.Add(MainForm.CreateLabel("选择本次运行项目", 18F, FontStyle.Bold, Theme.Text, 28, 20, 320, 38));
            Controls.Add(MainForm.CreateLabel("只执行勾选项目，未勾选项目不会消耗材料或文案次数", 9F, FontStyle.Regular, Theme.Muted, 30, 58, 440, 24));

            var card = new CardPanel { Location = new Point(24, 96), Size = new Size(456, 330), Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right };
            projectList = new CheckedListBox {
                Location = new Point(18, 18), Size = new Size(420, 292), Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right,
                BorderStyle = BorderStyle.None, CheckOnClick = true, BackColor = Color.White, ForeColor = Theme.Text,
                Font = new Font("Microsoft YaHei UI", 10F), ItemHeight = 36
            };
            foreach (string project in projects) projectList.Items.Add(project, true);
            card.Controls.Add(projectList);

            var all = new ModernButton { Text = "全选", IsOutline = true, FillColor = Color.White, Location = new Point(24, 444), Size = new Size(88, 38), Anchor = AnchorStyles.Bottom | AnchorStyles.Left };
            var none = new ModernButton { Text = "清空", IsOutline = true, FillColor = Color.White, Location = new Point(122, 444), Size = new Size(88, 38), Anchor = AnchorStyles.Bottom | AnchorStyles.Left };
            var cancel = new ModernButton { Text = "取消", IsOutline = true, FillColor = Color.White, Location = new Point(258, 444), Size = new Size(100, 38), Anchor = AnchorStyles.Bottom | AnchorStyles.Right };
            var start = new ModernButton { Text = "开始生成", IconGlyph = "\uE768", FillColor = Theme.Green, TextColor = Color.White, Location = new Point(370, 444), Size = new Size(110, 38), Anchor = AnchorStyles.Bottom | AnchorStyles.Right };
            all.Click += delegate { for (int i = 0; i < projectList.Items.Count; i++) projectList.SetItemChecked(i, true); };
            none.Click += delegate { for (int i = 0; i < projectList.Items.Count; i++) projectList.SetItemChecked(i, false); };
            cancel.Click += delegate { DialogResult = DialogResult.Cancel; Close(); };
            start.Click += delegate { SaveAndClose(); };
            Controls.Add(card); Controls.Add(all); Controls.Add(none); Controls.Add(cancel); Controls.Add(start);
        }

        private void SaveAndClose()
        {
            SelectedProjects = new List<string>();
            foreach (object item in projectList.CheckedItems) SelectedProjects.Add(Convert.ToString(item));
            if (SelectedProjects.Count == 0) {
                MessageBox.Show("请至少勾选一个项目。", "没有选择项目", MessageBoxButtons.OK, MessageBoxIcon.Information);
                return;
            }
            DialogResult = DialogResult.OK;
            Close();
        }
    }

    internal sealed class TaskPlanRule
    {
        public string Project { get; set; }
        public string Language { get; set; }
        public string Material { get; set; }
        public int Count { get; set; }
    }

    internal sealed class TaskPlanDialog : Form
    {
        private readonly DataGridView grid;
        private readonly string project;
        private readonly List<string> languages;
        private readonly List<string> materials;
        public List<TaskPlanRule> Rules { get; private set; }

        public TaskPlanDialog(string projectName, List<string> languageOptions, List<string> materialOptions, List<TaskPlanRule> rules)
        {
            project = projectName;
            languages = languageOptions;
            materials = materialOptions;
            Text = project + " · 语言材料数量计划";
            StartPosition = FormStartPosition.CenterParent;
            Size = new Size(760, 620);
            MinimumSize = new Size(680, 540);
            BackColor = Theme.Background;
            Font = new Font("Microsoft YaHei UI", 9F);

            Controls.Add(MainForm.CreateLabel(project + " · 每日数量计划", 18F, FontStyle.Bold, Theme.Text, 28, 20, 560, 38));
            Controls.Add(MainForm.CreateLabel("每行填写一种语言与材料的数量；保存后只重建当前项目，其他项目保持不变", 9F, FontStyle.Regular, Theme.Muted, 30, 58, 680, 24));
            var card = new CardPanel { Location = new Point(24, 96), Size = new Size(700, 390), Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right };
            grid = new DataGridView {
                Location = new Point(16, 16), Size = new Size(668, 314), Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right,
                BackgroundColor = Color.White, BorderStyle = BorderStyle.None, CellBorderStyle = DataGridViewCellBorderStyle.SingleHorizontal,
                GridColor = Theme.Border, EnableHeadersVisualStyles = false, ColumnHeadersBorderStyle = DataGridViewHeaderBorderStyle.None,
                ColumnHeadersHeight = 40, RowHeadersVisible = false, RowTemplate = { Height = 40 }, AllowUserToAddRows = false,
                AllowUserToDeleteRows = false, AllowUserToResizeRows = false, SelectionMode = DataGridViewSelectionMode.FullRowSelect,
                MultiSelect = true, EditMode = DataGridViewEditMode.EditOnKeystrokeOrF2
            };
            grid.ColumnHeadersDefaultCellStyle = new DataGridViewCellStyle { BackColor = Color.FromArgb(248, 250, 252), ForeColor = Theme.Muted, Font = new Font("Microsoft YaHei UI", 9F, FontStyle.Bold), Padding = new Padding(8, 0, 8, 0) };
            grid.DefaultCellStyle = new DataGridViewCellStyle { BackColor = Color.White, ForeColor = Theme.Text, SelectionBackColor = Color.FromArgb(239, 246, 255), SelectionForeColor = Theme.Text, Padding = new Padding(8, 4, 8, 4) };
            grid.Columns.Add(new DataGridViewComboBoxColumn { Name = "language", HeaderText = "语言", Width = 190, FlatStyle = FlatStyle.Flat });
            grid.Columns.Add(new DataGridViewComboBoxColumn { Name = "material", HeaderText = "材料", Width = 220, FlatStyle = FlatStyle.Flat });
            grid.Columns.Add(new DataGridViewTextBoxColumn { Name = "count", HeaderText = "数量", AutoSizeMode = DataGridViewAutoSizeColumnMode.Fill });
            foreach (string item in languages) ((DataGridViewComboBoxColumn)grid.Columns["language"]).Items.Add(item);
            foreach (string item in materials) ((DataGridViewComboBoxColumn)grid.Columns["material"]).Items.Add(item);
            foreach (TaskPlanRule rule in rules) grid.Rows.Add(rule.Language, rule.Material, rule.Count);
            grid.CurrentCellDirtyStateChanged += delegate { if (grid.IsCurrentCellDirty) grid.CommitEdit(DataGridViewDataErrorContexts.Commit); };
            grid.DataError += delegate { };

            var add = new ModernButton { Text = "添加组合", IconGlyph = "\uE710", FillColor = Theme.Blue, TextColor = Color.White, Location = new Point(16, 340), Size = new Size(112, 36), Anchor = AnchorStyles.Bottom | AnchorStyles.Left };
            var remove = new ModernButton { Text = "删除选中", IconGlyph = "\uE74D", IsOutline = true, TextColor = Theme.Red, FillColor = Color.White, Location = new Point(138, 340), Size = new Size(112, 36), Anchor = AnchorStyles.Bottom | AnchorStyles.Left };
            add.Click += delegate { AddRule(); };
            remove.Click += delegate { RemoveSelected(); };
            card.Controls.Add(grid); card.Controls.Add(add); card.Controls.Add(remove);

            var cancel = new ModernButton { Text = "取消", IsOutline = true, FillColor = Color.White, Location = new Point(452, 510), Size = new Size(110, 40), Anchor = AnchorStyles.Bottom | AnchorStyles.Right };
            var save = new ModernButton { Text = "保存并生成", IconGlyph = "\uE74E", FillColor = Theme.Green, TextColor = Color.White, Location = new Point(574, 510), Size = new Size(150, 40), Anchor = AnchorStyles.Bottom | AnchorStyles.Right };
            cancel.Click += delegate { DialogResult = DialogResult.Cancel; Close(); };
            save.Click += delegate { SaveAndClose(); };
            Controls.Add(card); Controls.Add(cancel); Controls.Add(save);
            if (grid.Rows.Count == 0) AddRule();
        }

        private void AddRule()
        {
            string language = languages.Count > 0 ? languages[0] : "";
            string material = materials.Count > 0 ? materials[0] : "";
            int index = grid.Rows.Add(language, material, 1);
            grid.CurrentCell = grid.Rows[index].Cells["count"];
            grid.BeginEdit(true);
        }

        private void RemoveSelected()
        {
            var indexes = new List<int>();
            foreach (DataGridViewRow row in grid.SelectedRows) indexes.Add(row.Index);
            indexes.Sort(); indexes.Reverse();
            foreach (int index in indexes) if (index >= 0 && index < grid.Rows.Count) grid.Rows.RemoveAt(index);
        }

        private void SaveAndClose()
        {
            grid.EndEdit();
            Rules = new List<TaskPlanRule>();
            var unique = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            int total = 0;
            foreach (DataGridViewRow row in grid.Rows) {
                string language = Convert.ToString(row.Cells["language"].Value).Trim();
                string material = Convert.ToString(row.Cells["material"].Value).Trim();
                int count;
                if (language.Length == 0 || material.Length == 0 || !Int32.TryParse(Convert.ToString(row.Cells["count"].Value), out count) || count <= 0) {
                    MessageBox.Show("每一行都必须选择语言、材料，并填写大于0的数量。", "计划无效", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                    return;
                }
                string key = language + "\u001f" + material;
                if (!unique.Add(key)) {
                    MessageBox.Show("语言“" + language + "”与材料“" + material + "”重复，请合并为一行。", "计划重复", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                    return;
                }
                Rules.Add(new TaskPlanRule { Project = project, Language = language, Material = material, Count = count });
                total += count;
            }
            if (total == 0) {
                MessageBox.Show("计划总数量必须大于0。", "计划为空", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }
            DialogResult = DialogResult.OK;
            Close();
        }
    }

    public sealed class MainForm : Form
    {
        private readonly string projectRoot;
        private readonly Panel contentHost;
        private readonly Panel canvas;
        private readonly Panel taskCanvas;
        private readonly List<ModernButton> navButtons = new List<ModernButton>();
        private List<string> projectOptions = new List<string>();
        private List<string> languageOptions = new List<string>();
        private Dictionary<string, string> languageVoiceRules = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        private Label runStatus;
        private Label targetValue;
        private Label taskValue;
        private Label browserValue;
        private Label modeValue;
        private Label sideBrowser;
        private Label sideVersion;
        private RichTextBox logBox;
        private ModernButton startButton;
        private ModernButton stopButton;
        private ProgressRing progressRing;
        private Label completedValue;
        private Label processingValue;
        private Label failedValue;
        private Label pendingValue;
        private CardPanel logCard;
        private CardPanel taskTableCard;
        private DataGridView taskGrid;
        private Label taskPageCount;
        private ListBox projectList;
        private Label selectedProjectLabel;
        private ComboBox projectMaterialSelector;
        private ModernButton applyProjectMaterialButton;
        private ModernButton rebuildFromPlanButton;
        private string selectedProject = "";
        private Process currentJob;
        private Process controlServer;
        private int totalTasks;
        private int completedTasks;
        private int failedTasks;
        private int processingTasks;
        private bool updatingTaskCombos;
        private bool applyingTaskPreview;
        private bool suppressImmediatePreview;
        private readonly HashSet<DataGridViewRow> previewRows = new HashSet<DataGridViewRow>();
        private readonly HashSet<DataGridViewRow> pendingPreviewRows = new HashSet<DataGridViewRow>();

        public MainForm()
        {
            projectRoot = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar);
            Text = "批量语音客户端";
            StartPosition = FormStartPosition.CenterScreen;
            MinimumSize = new Size(1120, 760);
            Size = new Size(1360, 920);
            BackColor = Theme.Background;
            Font = new Font("Microsoft YaHei UI", 9F);
            AutoScaleMode = AutoScaleMode.Dpi;

            Panel sidebar = BuildSidebar();
            Controls.Add(sidebar);

            contentHost = new Panel {
                Location = new Point(240, 0),
                Size = new Size(ClientSize.Width - 240, ClientSize.Height),
                Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right,
                BackColor = Theme.Background,
                AutoScroll = true
            };
            canvas = new Panel { Location = new Point(0, 0), BackColor = Theme.Background };
            contentHost.Controls.Add(canvas);
            LoadCatalogs();
            taskCanvas = BuildTaskPage();
            taskCanvas.Visible = false;
            contentHost.Controls.Add(taskCanvas);
            Controls.Add(contentHost);
            sidebar.BringToFront();

            CardPanel actionCard = BuildActionCard();
            CardPanel statusCard = BuildStatusCard();
            CardPanel progressCard = BuildProgressCard();
            logCard = BuildLogCard();
            Panel footer = BuildFooter();
            canvas.Controls.Add(actionCard);
            canvas.Controls.Add(statusCard);
            canvas.Controls.Add(progressCard);
            canvas.Controls.Add(logCard);
            canvas.Controls.Add(footer);

            contentHost.Resize += delegate { LayoutDashboard(); LayoutTaskPage(); };
            Shown += delegate {
                LayoutDashboard();
                LayoutTaskPage();
                LoadTasksIntoGrid();
                RefreshSummary();
                AppendLog("客户端已启动。配置加载完成，等待开始任务。\r\n");
            };
            FormClosing += OnFormClosing;
        }

        private Panel BuildSidebar()
        {
            var sidebar = new Panel { Dock = DockStyle.Left, Width = 240, BackColor = Color.White, Padding = new Padding(16, 22, 16, 18) };
            sidebar.Paint += delegate(object sender, PaintEventArgs e) { using (var pen = new Pen(Theme.Border)) e.Graphics.DrawLine(pen, sidebar.Width - 1, 0, sidebar.Width - 1, sidebar.Height); };

            var logo = new Label { Text = "▥", Font = new Font("Segoe UI Symbol", 22F, FontStyle.Bold), ForeColor = Theme.Blue, Location = new Point(20, 24), Size = new Size(42, 42), TextAlign = ContentAlignment.MiddleCenter };
            var name = MakeLabel("批量语音客户端", 14F, FontStyle.Bold, Theme.Text, 70, 25, 160, 26);
            var sub = MakeLabel("语音生成 · 自动化工具", 8.5F, FontStyle.Regular, Theme.Muted, 70, 54, 160, 20);
            sidebar.Controls.Add(logo); sidebar.Controls.Add(name); sidebar.Controls.Add(sub);

            string[] navNames = { "控制台", "任务管理", "日志", "设置", "关于" };
            string[] icons = { "\uE80F", "\uE9D5", "\uE8A5", "\uE713", "\uE946" };
            for (int i = 0; i < navNames.Length; i++) {
                var button = new ModernButton { Text = navNames[i], IconGlyph = icons[i], IsActive = i == 0, IsOutline = false, FillColor = Color.White, Location = new Point(16, 112 + i * 54), Size = new Size(208, 46), TextColor = Theme.Text };
                int index = i;
                button.Click += delegate {
                    if (index == 0) ShowDashboard(false);
                    else if (index == 1) ShowTaskManager();
                    else if (index == 2) ShowDashboard(true);
                    else if (index == 3) OpenControlPanel();
                    else ShowAbout();
                };
                navButtons.Add(button);
                sidebar.Controls.Add(button);
            }

            var sideCard = new CardPanel { Anchor = AnchorStyles.Left | AnchorStyles.Right | AnchorStyles.Bottom, Location = new Point(16, 586), Size = new Size(208, 156) };
            sideCard.Resize += delegate { sideCard.Location = new Point(16, sidebar.ClientSize.Height - sideCard.Height - 18); };
            runStatus = MakeLabel("●  空闲中", 9.5F, FontStyle.Bold, Theme.Green, 16, 16, 170, 24);
            sideBrowser = MakeLabel("浏览器    已安装", 8.5F, FontStyle.Regular, Theme.Muted, 16, 55, 176, 20);
            sideVersion = MakeLabel("版本        Chromium", 8.5F, FontStyle.Regular, Theme.Muted, 16, 82, 176, 20);
            var sideRefresh = new ModernButton { Text = "刷新信息", IconGlyph = "\uE72C", IsOutline = true, Location = new Point(12, 112), Size = new Size(184, 34), FillColor = Color.White, TextColor = Theme.Muted };
            sideRefresh.Click += delegate { RefreshSummary(); };
            sideCard.Controls.Add(runStatus); sideCard.Controls.Add(sideBrowser); sideCard.Controls.Add(sideVersion); sideCard.Controls.Add(sideRefresh);
            sidebar.Controls.Add(sideCard);
            return sidebar;
        }

        private Panel BuildTaskPage()
        {
            var page = new Panel { Location = new Point(0, 0), BackColor = Theme.Background };

            var header = new CardPanel { Name = "taskHeaderCard", Location = new Point(24, 24), Height = 142 };
            header.Controls.Add(MakeLabel("任务管理", 20F, FontStyle.Bold, Theme.Text, 24, 18, 300, 42));
            header.Controls.Add(MakeLabel("在客户端内编辑批量语音任务，保存后即可开始生成", 10F, FontStyle.Regular, Theme.Muted, 26, 61, 560, 26));
            taskPageCount = MakeLabel("0 条任务", 9F, FontStyle.Bold, Theme.Blue, 26, 101, 150, 24);
            header.Controls.Add(taskPageCount);

            var plan = new ModernButton { Name = "taskPlanButton", Text = "数量计划", IconGlyph = "\uE9D5", IsOutline = true, FillColor = Color.White, Location = new Point(361, 83), Size = new Size(112, 42) };
            var catalog = new ModernButton { Name = "catalogTaskButton", Text = "管理项目/语言", IconGlyph = "\uE8EC", IsOutline = true, FillColor = Color.White, Location = new Point(483, 83), Size = new Size(148, 42) };
            var outputDirs = new ModernButton { Name = "outputDirsTaskButton", Text = "输出目录", IconGlyph = "\uE8B7", IsOutline = true, FillColor = Color.White, Location = new Point(495, 83), Size = new Size(124, 42) };
            var add = new ModernButton { Name = "addTaskButton", Text = "添加任务", IconGlyph = "\uE710", FillColor = Theme.Blue, TextColor = Color.White, Location = new Point(495, 83), Size = new Size(124, 42) };
            var remove = new ModernButton { Name = "removeTaskButton", Text = "删除选中", IconGlyph = "\uE74D", IsOutline = true, OutlineColor = Color.FromArgb(254, 202, 202), TextColor = Theme.Red, FillColor = Color.White, Location = new Point(629, 83), Size = new Size(124, 42) };
            var import = new ModernButton { Name = "importTaskButton", Text = "导入 CSV", IconGlyph = "\uE8B5", IsOutline = true, FillColor = Color.White, Location = new Point(763, 83), Size = new Size(124, 42) };
            var reload = new ModernButton { Name = "reloadTaskButton", Text = "重新加载", IconGlyph = "\uE72C", IsOutline = true, FillColor = Color.White, Location = new Point(897, 83), Size = new Size(124, 42) };
            var save = new ModernButton { Name = "saveTaskButton", Text = "保存任务", IconGlyph = "\uE74E", FillColor = Theme.Green, TextColor = Color.White, Location = new Point(1031, 83), Size = new Size(124, 42) };
            plan.Click += delegate { OpenTaskPlanEditor(); };
            catalog.Click += delegate { OpenCatalogManager(); };
            outputDirs.Click += delegate { ConfigureProjectOutputDirectories(); };
            add.Click += delegate { AddTaskRow(); };
            remove.Click += delegate { DeleteSelectedTasks(); };
            import.Click += delegate { ImportTasks(); };
            reload.Click += delegate { LoadTasksIntoGrid(); };
            save.Click += delegate { SaveTasksFromGrid(); };
            header.Controls.Add(plan); header.Controls.Add(catalog); header.Controls.Add(outputDirs); header.Controls.Add(add); header.Controls.Add(remove); header.Controls.Add(import); header.Controls.Add(reload); header.Controls.Add(save);

            taskTableCard = new CardPanel { Name = "taskTableCard", Location = new Point(24, 182), Height = 650 };
            taskTableCard.Controls.Add(MakeLabel("项目", 11F, FontStyle.Bold, Theme.Text, 22, 16, 180, 28));
            selectedProjectLabel = MakeLabel("项目文案", 11F, FontStyle.Bold, Theme.Text, 258, 16, 180, 28);
            taskTableCard.Controls.Add(selectedProjectLabel);
            var selectAllTasks = new ModernButton { Name = "selectAllTasksButton", Text = "全选", IsOutline = true, FillColor = Color.White, Location = new Point(446, 9), Size = new Size(82, 34) };
            var clearTaskSelection = new ModernButton { Name = "clearTaskSelectionButton", Text = "清空", IsOutline = true, FillColor = Color.White, Location = new Point(536, 9), Size = new Size(96, 34) };
            selectAllTasks.Click += delegate { SetVisibleTaskChecks(true); };
            clearTaskSelection.Click += delegate { SetVisibleTaskChecks(false); };
            taskTableCard.Controls.Add(selectAllTasks); taskTableCard.Controls.Add(clearTaskSelection);
            var unifiedLabel = MakeLabel("统一材料", 9F, FontStyle.Bold, Theme.Muted, 450, 16, 78, 26);
            unifiedLabel.Name = "unifiedMaterialLabel";
            projectMaterialSelector = new ComboBox { Name = "projectMaterialSelector", Location = new Point(530, 12), Size = new Size(170, 30), DropDownStyle = ComboBoxStyle.DropDownList, FlatStyle = FlatStyle.Flat, Font = new Font("Microsoft YaHei UI", 9F) };
            applyProjectMaterialButton = new ModernButton { Name = "applyProjectMaterialButton", Text = "应用全部", IconGlyph = "\uE73E", FillColor = Theme.Blue, TextColor = Color.White, Location = new Point(712, 9), Size = new Size(116, 34) };
            applyProjectMaterialButton.Click += delegate { ApplyUnifiedMaterial(); };
            rebuildFromPlanButton = new ModernButton { Name = "rebuildFromPlanButton", Text = "按计划重建", IconGlyph = "\uE72C", IsOutline = true, FillColor = Color.White, Location = new Point(840, 9), Size = new Size(128, 34) };
            rebuildFromPlanButton.Click += delegate { RebuildSelectedProjectFromSavedPlan(); };
            taskTableCard.Controls.Add(unifiedLabel); taskTableCard.Controls.Add(projectMaterialSelector); taskTableCard.Controls.Add(applyProjectMaterialButton); taskTableCard.Controls.Add(rebuildFromPlanButton);

            projectList = new ListBox {
                Location = new Point(14, 54),
                Size = new Size(218, 570),
                Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left,
                BorderStyle = BorderStyle.None,
                BackColor = Color.White,
                ForeColor = Theme.Text,
                Font = new Font("Microsoft YaHei UI", 9.5F),
                DrawMode = DrawMode.OwnerDrawFixed,
                ItemHeight = 44,
                IntegralHeight = false
            };
            projectList.DrawItem += DrawProjectItem;
            projectList.SelectedIndexChanged += delegate { SelectProject(Convert.ToString(projectList.SelectedItem)); };
            var projectDivider = new Panel { Name = "projectDivider", Location = new Point(240, 52), Size = new Size(1, 572), Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left, BackColor = Theme.Border };
            taskTableCard.Controls.Add(projectList);
            taskTableCard.Controls.Add(projectDivider);

            taskGrid = new DataGridView {
                Location = new Point(258, 56),
                Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right,
                Size = new Size(1050, 568),
                BackgroundColor = Color.White,
                BorderStyle = BorderStyle.None,
                CellBorderStyle = DataGridViewCellBorderStyle.SingleHorizontal,
                GridColor = Theme.Border,
                EnableHeadersVisualStyles = false,
                ColumnHeadersBorderStyle = DataGridViewHeaderBorderStyle.None,
                ColumnHeadersHeight = 42,
                RowHeadersVisible = false,
                RowTemplate = { Height = 44 },
                AllowUserToAddRows = false,
                AllowUserToDeleteRows = false,
                AllowUserToResizeRows = false,
                MultiSelect = true,
                SelectionMode = DataGridViewSelectionMode.FullRowSelect,
                AutoSizeColumnsMode = DataGridViewAutoSizeColumnsMode.None,
                EditMode = DataGridViewEditMode.EditOnKeystrokeOrF2,
                Font = new Font("Microsoft YaHei UI", 9F)
            };
            taskGrid.ColumnHeadersDefaultCellStyle = new DataGridViewCellStyle { BackColor = Color.FromArgb(248, 250, 252), ForeColor = Theme.Muted, Font = new Font("Microsoft YaHei UI", 9F, FontStyle.Bold), Alignment = DataGridViewContentAlignment.MiddleLeft, Padding = new Padding(8, 0, 8, 0), SelectionBackColor = Color.FromArgb(248, 250, 252), SelectionForeColor = Theme.Muted };
            taskGrid.DefaultCellStyle = new DataGridViewCellStyle { BackColor = Color.White, ForeColor = Theme.Text, SelectionBackColor = Color.FromArgb(239, 246, 255), SelectionForeColor = Theme.Text, Padding = new Padding(8, 5, 8, 5), WrapMode = DataGridViewTriState.False };
            taskGrid.AlternatingRowsDefaultCellStyle = new DataGridViewCellStyle { BackColor = Color.FromArgb(251, 252, 254), ForeColor = Theme.Text, SelectionBackColor = Color.FromArgb(239, 246, 255), SelectionForeColor = Theme.Text, Padding = new Padding(8, 5, 8, 5) };
            taskGrid.Columns.Add(new DataGridViewCheckBoxColumn {
                Name = "runSelected", HeaderText = "生成", Width = 54, MinimumWidth = 48,
                FalseValue = false, TrueValue = true, ThreeState = false,
                SortMode = DataGridViewColumnSortMode.NotSortable, Frozen = true
            });
            AddTaskColumn("id", "编号", 60);
            AddTaskColumn("project", "项目", 120);
            AddTaskColumn("language", "语言", 85);
            AddTaskColumn("material", "指定材料", 105);
            AddTaskColumn("materialPreview", "文案材料", 95);
            AddTaskColumn("voice", "声音", 90);
            AddTaskColumn("text", "文本内容", 220);
            AddTaskColumn("outputName", "输出文件名", 135);
            AddTaskColumn("status", "状态", 70);
            taskGrid.Columns["project"].Visible = false;
            taskGrid.Columns["materialPreview"].ReadOnly = true;
            taskGrid.Columns["materialPreview"].DefaultCellStyle.ForeColor = Theme.Blue;
            taskGrid.RowsAdded += delegate { UpdateTaskPageCount(); };
            taskGrid.RowsRemoved += delegate { UpdateTaskPageCount(); };
            taskGrid.CurrentCellDirtyStateChanged += delegate { if (taskGrid.IsCurrentCellDirty) taskGrid.CommitEdit(DataGridViewDataErrorContexts.Commit); };
            taskGrid.CellValueChanged += OnTaskCellValueChanged;
            taskGrid.CellFormatting += OnTaskCellFormatting;
            taskGrid.DataError += delegate { };
            taskGrid.SizeChanged += delegate { UpdateTaskGridLayout(); };
            taskTableCard.Controls.Add(taskGrid);
            page.Controls.Add(header);
            page.Controls.Add(taskTableCard);
            return page;
        }

        private void AddTaskColumn(string name, string title, float weight)
        {
            var column = new DataGridViewTextBoxColumn { Name = name, HeaderText = title, FillWeight = weight, SortMode = DataGridViewColumnSortMode.NotSortable };
            taskGrid.Columns.Add(column);
        }

        private void SetVisibleTaskChecks(bool selected)
        {
            taskGrid.EndEdit();
            foreach (DataGridViewRow row in taskGrid.Rows) {
                if (!row.Visible) continue;
                bool done = String.Equals(Convert.ToString(row.Cells["status"].Value).Trim(), "done", StringComparison.OrdinalIgnoreCase);
                row.Cells["runSelected"].Value = selected && !done;
            }
            taskGrid.InvalidateColumn(taskGrid.Columns["runSelected"].Index);
        }

        private void UpdateTaskGridLayout()
        {
            if (taskGrid == null || taskGrid.Columns.Count == 0) return;
            int width = taskGrid.ClientSize.Width;
            taskGrid.Columns["runSelected"].Width = 54;
            taskGrid.Columns["id"].Width = 70;
            taskGrid.Columns["language"].Width = width < 900 ? 82 : 100;
            taskGrid.Columns["material"].Width = width < 900 ? 96 : 118;
            taskGrid.Columns["materialPreview"].Visible = width >= 980;
            taskGrid.Columns["materialPreview"].Width = 105;
            taskGrid.Columns["voice"].Width = width < 900 ? 92 : 125;
            taskGrid.Columns["status"].Width = 78;
            taskGrid.Columns["outputName"].Visible = width >= 820;
            taskGrid.Columns["text"].MinimumWidth = width < 900 ? 130 : 190;
            taskGrid.Columns["text"].AutoSizeMode = DataGridViewAutoSizeColumnMode.Fill;
            taskGrid.Columns["text"].FillWeight = 62;
            taskGrid.Columns["outputName"].MinimumWidth = 110;
            taskGrid.Columns["outputName"].AutoSizeMode = DataGridViewAutoSizeColumnMode.Fill;
            taskGrid.Columns["outputName"].FillWeight = 38;
        }

        private CardPanel BuildActionCard()
        {
            var card = new CardPanel { Name = "actionCard", Location = new Point(24, 24), Height = 220 };
            card.Controls.Add(MakeLabel("批量语音生成控制台", 20F, FontStyle.Bold, Theme.Text, 24, 18, 450, 42));
            card.Controls.Add(MakeLabel("通过 Playwright 自动化批量生成语音并下载", 10F, FontStyle.Regular, Theme.Muted, 26, 61, 520, 26));
            var divider = new Panel { Location = new Point(1, 100), Height = 1, BackColor = Theme.Border, Anchor = AnchorStyles.Left | AnchorStyles.Right, Width = 900 };
            card.Controls.Add(divider);

            startButton = new ModernButton { Text = "开始生成", Subtitle = "启动批量任务", IconGlyph = "\uE768", FillColor = Theme.Green, TextColor = Color.White, Location = new Point(24, 121), Size = new Size(205, 76) };
            stopButton = new ModernButton { Text = "停止任务", Subtitle = "停止当前任务", IconGlyph = "\uE71A", FillColor = Theme.Red, TextColor = Color.White, Location = new Point(245, 121), Size = new Size(205, 76), Enabled = false };
            var console = new ModernButton { Text = "打开控制台", Subtitle = "配置网站与任务", IconGlyph = "\uE7F4", IsOutline = true, Location = new Point(466, 121), Size = new Size(180, 76), FillColor = Color.White };
            var downloads = new ModernButton { Text = "打开下载目录", Subtitle = "查看生成文件", IconGlyph = "\uE8B7", IsOutline = true, Location = new Point(662, 121), Size = new Size(180, 76), FillColor = Color.White };
            var refresh = new ModernButton { Text = "刷新信息", Subtitle = "更新状态信息", IconGlyph = "\uE72C", IsOutline = true, Location = new Point(858, 121), Size = new Size(170, 76), FillColor = Color.White };
            startButton.Click += delegate { StartGeneration(); };
            stopButton.Click += delegate { StopGeneration(); };
            console.Click += delegate { OpenControlPanel(); };
            downloads.Click += delegate { OpenDownloads(); };
            refresh.Click += delegate { RefreshSummary(); };
            card.Controls.Add(startButton); card.Controls.Add(stopButton); card.Controls.Add(console); card.Controls.Add(downloads); card.Controls.Add(refresh);
            return card;
        }

        private CardPanel BuildStatusCard()
        {
            var card = new CardPanel { Name = "statusCard", Height = 118 };
            targetValue = AddStatusItem(card, 24, "\uE774", "目标网页", "未配置", Theme.Blue);
            taskValue = AddStatusItem(card, 285, "\uE9D5", "任务数量", "0", Theme.Blue);
            browserValue = AddStatusItem(card, 530, "\uE73E", "浏览器状态", "检查中", Theme.Green);
            modeValue = AddStatusItem(card, 775, "\uE7F4", "运行模式", "可视化模式", Color.FromArgb(124, 58, 237));
            return card;
        }

        private Label AddStatusItem(Control parent, int x, string icon, string caption, string value, Color color)
        {
            var iconLabel = new Label { Text = icon, Font = new Font("Segoe MDL2 Assets", 17F), ForeColor = color, BackColor = Drawing.Blend(color, Color.White, 0.88F), Location = new Point(x, 30), Size = new Size(48, 48), TextAlign = ContentAlignment.MiddleCenter };
            using (var circle = new GraphicsPath()) {
                circle.AddEllipse(0, 0, 48, 48);
                iconLabel.Region = new Region(circle);
            }
            var cap = MakeLabel(caption, 8.5F, FontStyle.Regular, Theme.Muted, x + 62, 25, 190, 22);
            var val = MakeLabel(value, 11F, FontStyle.Bold, Theme.Text, x + 62, 49, 210, 27);
            parent.Controls.Add(iconLabel); parent.Controls.Add(cap); parent.Controls.Add(val);
            return val;
        }

        private CardPanel BuildProgressCard()
        {
            var card = new CardPanel { Name = "progressCard", Height = 158 };
            card.Controls.Add(MakeLabel("任务进度", 11F, FontStyle.Bold, Theme.Text, 22, 14, 180, 28));
            progressRing = new ProgressRing { Location = new Point(38, 51) };
            card.Controls.Add(progressRing);
            completedValue = AddMetric(card, 235, "已完成", "0", Theme.Green);
            processingValue = AddMetric(card, 445, "处理中", "0", Theme.Blue);
            failedValue = AddMetric(card, 655, "失败", "0", Theme.Red);
            pendingValue = AddMetric(card, 865, "待处理", "0", Theme.Text);
            return card;
        }

        private Label AddMetric(Control parent, int x, string caption, string value, Color color)
        {
            parent.Controls.Add(MakeLabel(caption, 8.5F, FontStyle.Regular, Theme.Muted, x, 60, 120, 22));
            var label = MakeLabel(value, 16F, FontStyle.Bold, color, x, 85, 120, 34);
            parent.Controls.Add(label);
            return label;
        }

        private CardPanel BuildLogCard()
        {
            var card = new CardPanel { Name = "logCard", Height = 258 };
            card.Controls.Add(MakeLabel("实时日志", 11F, FontStyle.Bold, Theme.Text, 22, 13, 130, 30));
            card.Controls.Add(MakeLabel("●  实时更新中", 8.5F, FontStyle.Regular, Theme.Green, 125, 17, 130, 22));
            var clear = new ModernButton { Name = "clearLogButton", Text = "清空日志", IconGlyph = "\uE74D", IsOutline = true, Location = new Point(815, 10), Size = new Size(112, 36), FillColor = Color.White };
            var save = new ModernButton { Name = "saveLogButton", Text = "保存日志", IconGlyph = "\uE74E", IsOutline = true, Location = new Point(937, 10), Size = new Size(112, 36), FillColor = Color.White };
            clear.Click += delegate { logBox.Clear(); };
            save.Click += delegate { SaveLog(); };
            logBox = new RichTextBox { Location = new Point(22, 56), Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right, Size = new Size(1027, 180), ReadOnly = true, BackColor = Theme.Dark, ForeColor = Color.FromArgb(226, 232, 240), BorderStyle = BorderStyle.None, Font = new Font("Consolas", 9.5F), DetectUrls = false, Padding = new Padding(12) };
            card.Controls.Add(clear); card.Controls.Add(save); card.Controls.Add(logBox);
            return card;
        }

        private Panel BuildFooter()
        {
            var footer = new Panel { Name = "footer", Height = 52, BackColor = Theme.Background };
            footer.Controls.Add(MakeLabel("批量语音客户端 v1.0.0   |   让语音生成更简单、更高效", 8.5F, FontStyle.Regular, Theme.Muted, 8, 17, 520, 22));
            var help = new ModernButton { Name = "helpButton", Text = "帮助文档", IconGlyph = "\uE8A5", Location = new Point(900, 8), Size = new Size(132, 36), FillColor = Theme.Background, TextColor = Theme.Muted };
            help.Click += delegate { OpenHelp(); };
            footer.Controls.Add(help);
            return footer;
        }

        private void LayoutDashboard()
        {
            int width = Math.Max(840, contentHost.ClientSize.Width - 48);
            canvas.Size = new Size(width + 48, 954);
            Control action = canvas.Controls["actionCard"];
            Control status = canvas.Controls["statusCard"];
            Control progress = canvas.Controls["progressCard"];
            Control footer = canvas.Controls["footer"];
            action.SetBounds(24, 24, width, 220);
            status.SetBounds(24, 260, width, 118);
            progress.SetBounds(24, 394, width, 158);
            logCard.SetBounds(24, 568, width, 306);
            footer.SetBounds(24, 890, width, 52);
            logCard.Controls["clearLogButton"].Left = width - 258;
            logCard.Controls["saveLogButton"].Left = width - 136;
            footer.Controls["helpButton"].Left = width - 140;
        }

        private void LayoutTaskPage()
        {
            int width = Math.Max(840, contentHost.ClientSize.Width - 48);
            taskCanvas.Size = new Size(width + 48, 860);
            Control header = taskCanvas.Controls["taskHeaderCard"];
            header.SetBounds(24, 24, width, 142);
            int gap = 8;
            string[] names = { "taskPlanButton", "catalogTaskButton", "outputDirsTaskButton", "addTaskButton", "removeTaskButton", "importTaskButton", "reloadTaskButton", "saveTaskButton" };
            int[] widths = width >= 1180
                ? new[] { 112, 144, 116, 106, 106, 106, 106, 106 }
                : new[] { 100, 120, 100, 88, 88, 88, 88, 88 };
            string[] fullTexts = { "数量计划", "管理项目/语言", "输出目录", "添加任务", "删除选中", "导入 CSV", "重新加载", "保存任务" };
            string[] compactTexts = { "计划", "项目/语言", "目录", "添加", "删除", "导入", "重载", "保存" };
            int totalWidth = gap * (names.Length - 1);
            foreach (int itemWidth in widths) totalWidth += itemWidth;
            int x = width - 24 - totalWidth;
            for (int i = 0; i < names.Length; i++) {
                header.Controls[names[i]].Width = widths[i];
                header.Controls[names[i]].Left = x;
                header.Controls[names[i]].Text = width >= 1180 ? fullTexts[i] : compactTexts[i];
                x += widths[i] + gap;
            }
            taskTableCard.SetBounds(24, 182, width, 650);
            bool compact = width < 1120;
            Control unified = taskTableCard.Controls["unifiedMaterialLabel"];
            Control selectAllTasks = taskTableCard.Controls["selectAllTasksButton"];
            Control clearTaskSelection = taskTableCard.Controls["clearTaskSelectionButton"];
            unified.Visible = !compact;
            projectMaterialSelector.Visible = !compact;
            applyProjectMaterialButton.Visible = !compact;
            rebuildFromPlanButton.Visible = !compact;
            selectAllTasks.Left = compact ? width - 204 : 446;
            clearTaskSelection.Left = compact ? width - 114 : 536;
            if (!compact) {
                unified.Left = width - 552;
                projectMaterialSelector.Left = width - 466;
                applyProjectMaterialButton.Left = width - 280;
                rebuildFromPlanButton.Left = width - 140;
            }
            int projectWidth = width < 1020 ? 170 : 218;
            int gridLeft = 14 + projectWidth + 26;
            projectList.Width = projectWidth;
            taskTableCard.Controls["projectDivider"].Left = 14 + projectWidth + 8;
            selectedProjectLabel.Left = gridLeft;
            taskGrid.SetBounds(gridLeft, 56, width - gridLeft - 22, 568);
            UpdateTaskGridLayout();
        }

        private void ShowDashboard(bool jumpToLog)
        {
            taskCanvas.Visible = false;
            canvas.Visible = true;
            SetActiveNav(jumpToLog ? 2 : 0);
            contentHost.AutoScrollPosition = jumpToLog ? new Point(0, logCard.Top) : new Point(0, 0);
        }

        private void ShowTaskManager()
        {
            canvas.Visible = false;
            taskCanvas.Visible = true;
            taskCanvas.BringToFront();
            SetActiveNav(1);
            contentHost.AutoScrollPosition = new Point(0, 0);
            RefreshProjectList();
        }

        private void SetActiveNav(int activeIndex)
        {
            for (int i = 0; i < navButtons.Count; i++) {
                navButtons[i].IsActive = i == activeIndex;
                navButtons[i].Invalidate();
            }
        }

        private void RefreshProjectList()
        {
            if (projectList == null || taskGrid == null) return;
            string previous = selectedProject;
            var projects = new List<string>();
            foreach (string project in projectOptions) if (!projects.Contains(project)) projects.Add(project);
            foreach (DataGridViewRow row in taskGrid.Rows) {
                string project = Convert.ToString(row.Cells["project"].Value);
                if (project.Length > 0 && !projects.Contains(project)) projects.Add(project);
            }

            projectList.BeginUpdate();
            projectList.Items.Clear();
            foreach (string project in projects) projectList.Items.Add(project);
            projectList.EndUpdate();

            int selectedIndex = previous.Length > 0 ? projectList.Items.IndexOf(previous) : -1;
            if (selectedIndex < 0 && projectList.Items.Count > 0) selectedIndex = 0;
            projectList.SelectedIndex = selectedIndex;
            if (selectedIndex < 0) SelectProject("");
            projectList.Invalidate();
        }

        private void SelectProject(string project)
        {
            selectedProject = project ?? "";
            if (taskGrid == null) return;
            taskGrid.CurrentCell = null;
            foreach (DataGridViewRow row in taskGrid.Rows) {
                row.Visible = selectedProject.Length > 0 && String.Equals(Convert.ToString(row.Cells["project"].Value), selectedProject, StringComparison.OrdinalIgnoreCase);
            }
            if (selectedProjectLabel != null) selectedProjectLabel.Text = selectedProject.Length > 0 ? selectedProject + " · 语言文案" : "请选择项目";
            RefreshUnifiedMaterialSelector();
            UpdateTaskPageCount();
        }

        private void RefreshUnifiedMaterialSelector()
        {
            if (projectMaterialSelector == null) return;
            string common = null;
            bool mixed = false;
            foreach (DataGridViewRow row in taskGrid.Rows) {
                if (!String.Equals(Convert.ToString(row.Cells["project"].Value), selectedProject, StringComparison.OrdinalIgnoreCase)) continue;
                string value = Convert.ToString(row.Cells["material"].Value);
                if (String.IsNullOrWhiteSpace(value)) value = "自动随机";
                if (common == null) common = value;
                else if (!String.Equals(common, value, StringComparison.OrdinalIgnoreCase)) mixed = true;
            }
            projectMaterialSelector.BeginUpdate();
            projectMaterialSelector.Items.Clear();
            projectMaterialSelector.Items.Add("选择统一材料…");
            foreach (string item in MaterialOptionsForProject(selectedProject)) projectMaterialSelector.Items.Add(item);
            projectMaterialSelector.EndUpdate();
            int index = !mixed && common != null ? projectMaterialSelector.Items.IndexOf(common) : -1;
            projectMaterialSelector.SelectedIndex = index > 0 ? index : 0;
            projectMaterialSelector.Enabled = selectedProject.Length > 0;
            applyProjectMaterialButton.Enabled = selectedProject.Length > 0;
        }

        private void ApplyUnifiedMaterial()
        {
            if (selectedProject.Length == 0 || projectMaterialSelector.SelectedIndex <= 0) {
                MessageBox.Show("请先选择项目和统一材料。", "尚未选择", MessageBoxButtons.OK, MessageBoxIcon.Information);
                return;
            }
            string material = Convert.ToString(projectMaterialSelector.SelectedItem);
            int changed = 0;
            suppressImmediatePreview = true;
            try {
                foreach (DataGridViewRow row in taskGrid.Rows) {
                    if (!String.Equals(Convert.ToString(row.Cells["project"].Value), selectedProject, StringComparison.OrdinalIgnoreCase)) continue;
                    row.Cells["material"].Value = material;
                    changed++;
                }
                SaveTasksFromGrid();
            } finally { suppressImmediatePreview = false; }
            RefreshUnifiedMaterialSelector();
            AppendLog("已为项目“" + selectedProject + "”的 " + changed + " 条任务统一设置材料：“" + material + "”。\r\n");
        }

        private string TaskPlanFile()
        {
            return Path.Combine(projectRoot, "data", "project_task_plans.csv");
        }

        private List<TaskPlanRule> LoadTaskPlans()
        {
            var result = new List<TaskPlanRule>();
            string file = TaskPlanFile();
            if (!File.Exists(file)) return result;
            List<string[]> records = ParseCsv(File.ReadAllText(file, Encoding.UTF8));
            if (records.Count == 0) return result;
            var columns = new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase);
            for (int i = 0; i < records[0].Length; i++) columns[records[0][i].Trim().TrimStart('\uFEFF')] = i;
            int projectIndex, languageIndex, materialIndex, countIndex;
            if (!columns.TryGetValue("project", out projectIndex) || !columns.TryGetValue("language", out languageIndex) ||
                !columns.TryGetValue("material", out materialIndex) || !columns.TryGetValue("count", out countIndex)) return result;
            for (int i = 1; i < records.Count; i++) {
                string[] row = records[i];
                if (row.Length <= Math.Max(Math.Max(projectIndex, languageIndex), Math.Max(materialIndex, countIndex))) continue;
                int count;
                if (!Int32.TryParse(row[countIndex], out count) || count <= 0) continue;
                result.Add(new TaskPlanRule { Project = row[projectIndex], Language = row[languageIndex], Material = row[materialIndex], Count = count });
            }
            return result;
        }

        private void SaveTaskPlans(List<TaskPlanRule> rules)
        {
            var lines = new List<string> { "project,language,material,count" };
            foreach (TaskPlanRule rule in rules) {
                lines.Add(String.Join(",", new[] { CsvEscape(rule.Project), CsvEscape(rule.Language), CsvEscape(rule.Material), rule.Count.ToString() }));
            }
            string file = TaskPlanFile();
            Directory.CreateDirectory(Path.GetDirectoryName(file));
            File.WriteAllText(file, String.Join("\r\n", lines.ToArray()) + "\r\n", new UTF8Encoding(true));
        }

        private List<TaskPlanRule> PlanFromCurrentTasks(string project)
        {
            var counts = new Dictionary<string, TaskPlanRule>(StringComparer.OrdinalIgnoreCase);
            foreach (DataGridViewRow row in taskGrid.Rows) {
                if (!String.Equals(Convert.ToString(row.Cells["project"].Value), project, StringComparison.OrdinalIgnoreCase)) continue;
                string language = Convert.ToString(row.Cells["language"].Value).Trim();
                string material = Convert.ToString(row.Cells["material"].Value).Trim();
                if (material.Length == 0) material = "自动随机";
                string key = language + "\u001f" + material;
                TaskPlanRule rule;
                if (!counts.TryGetValue(key, out rule)) {
                    rule = new TaskPlanRule { Project = project, Language = language, Material = material, Count = 0 };
                    counts[key] = rule;
                }
                rule.Count++;
            }
            return new List<TaskPlanRule>(counts.Values);
        }

        private List<string> MaterialPlanOptions(string project)
        {
            return new List<string>(MaterialOptionsForProject(project));
        }

        private void OpenTaskPlanEditor()
        {
            if (selectedProject.Length == 0) {
                MessageBox.Show("请先选择一个项目。", "没有选择项目", MessageBoxButtons.OK, MessageBoxIcon.Information);
                return;
            }
            List<TaskPlanRule> allRules = LoadTaskPlans();
            List<TaskPlanRule> projectRules = allRules.FindAll(rule => String.Equals(rule.Project, selectedProject, StringComparison.OrdinalIgnoreCase));
            if (projectRules.Count == 0) projectRules = PlanFromCurrentTasks(selectedProject);
            using (var dialog = new TaskPlanDialog(selectedProject, new List<string>(languageOptions), MaterialPlanOptions(selectedProject), projectRules)) {
                if (dialog.ShowDialog(this) != DialogResult.OK) return;
                allRules.RemoveAll(rule => String.Equals(rule.Project, selectedProject, StringComparison.OrdinalIgnoreCase));
                allRules.AddRange(dialog.Rules);
                SaveTaskPlans(allRules);
                RebuildProjectFromPlan(selectedProject, dialog.Rules, false);
            }
        }

        private void RebuildSelectedProjectFromSavedPlan()
        {
            if (selectedProject.Length == 0) {
                MessageBox.Show("请先选择一个项目。", "没有选择项目", MessageBoxButtons.OK, MessageBoxIcon.Information);
                return;
            }
            List<TaskPlanRule> rules = LoadTaskPlans().FindAll(rule => String.Equals(rule.Project, selectedProject, StringComparison.OrdinalIgnoreCase));
            if (rules.Count == 0) {
                MessageBox.Show("这个项目还没有数量计划，请先点击“数量计划”保存一次。", "没有计划", MessageBoxButtons.OK, MessageBoxIcon.Information);
                return;
            }
            RebuildProjectFromPlan(selectedProject, rules, true);
        }

        private void RebuildProjectFromPlan(string project, List<TaskPlanRule> rules, bool confirm)
        {
            int total = 0;
            foreach (TaskPlanRule rule in rules) total += rule.Count;
            if (confirm) {
                DialogResult answer = MessageBox.Show("按已保存计划重建“" + project + "”的 " + total + " 条今日任务？\r\n只替换当前项目，其他项目不会改变。", "重建今日任务", MessageBoxButtons.YesNo, MessageBoxIcon.Question);
                if (answer != DialogResult.Yes) return;
            }
            suppressImmediatePreview = true;
            applyingTaskPreview = true;
            try {
                for (int index = taskGrid.Rows.Count - 1; index >= 0; index--) {
                    DataGridViewRow row = taskGrid.Rows[index];
                    if (String.Equals(Convert.ToString(row.Cells["project"].Value), project, StringComparison.OrdinalIgnoreCase)) {
                        previewRows.Remove(row);
                        pendingPreviewRows.Remove(row);
                        taskGrid.Rows.RemoveAt(index);
                    }
                }
                int sequence = 1;
                string prefix = DateTime.Now.ToString("MMddHHmmss") + "-" + project + "-";
                foreach (TaskPlanRule rule in rules) {
                    for (int item = 0; item < rule.Count; item++) {
                        string id = prefix + sequence.ToString("000");
                        int rowIndex = taskGrid.Rows.Add(true, id, project, rule.Language, rule.Material, "", VoiceForLanguage(rule.Language), "", "", "");
                        ConfigureTaskRow(taskGrid.Rows[rowIndex]);
                        sequence++;
                    }
                }
            } finally {
                applyingTaskPreview = false;
                suppressImmediatePreview = false;
            }
            SaveTasksFromGrid();
            SelectProject(project);
            AppendLog("已按数量计划重建项目“" + project + "”，共 " + total + " 条今日任务。\r\n");
        }

        private int ProjectTaskCount(string project)
        {
            int count = 0;
            if (taskGrid == null) return count;
            foreach (DataGridViewRow row in taskGrid.Rows) if (String.Equals(Convert.ToString(row.Cells["project"].Value), project, StringComparison.OrdinalIgnoreCase)) count++;
            return count;
        }

        private void DrawProjectItem(object sender, DrawItemEventArgs e)
        {
            if (e.Index < 0) return;
            bool selected = (e.State & DrawItemState.Selected) == DrawItemState.Selected;
            Color background = selected ? Color.FromArgb(235, 243, 255) : Color.White;
            using (var brush = new SolidBrush(background)) e.Graphics.FillRectangle(brush, e.Bounds);
            string project = Convert.ToString(projectList.Items[e.Index]);
            Color textColor = selected ? Theme.Blue : Theme.Text;
            TextRenderer.DrawText(e.Graphics, project, new Font("Microsoft YaHei UI", 9.5F, selected ? FontStyle.Bold : FontStyle.Regular), new Rectangle(e.Bounds.X + 12, e.Bounds.Y, e.Bounds.Width - 58, e.Bounds.Height), textColor, TextFormatFlags.VerticalCenter | TextFormatFlags.Left | TextFormatFlags.EndEllipsis);
            TextRenderer.DrawText(e.Graphics, ProjectTaskCount(project).ToString(), new Font("Microsoft YaHei UI", 8.5F, FontStyle.Bold), new Rectangle(e.Bounds.Right - 44, e.Bounds.Y, 30, e.Bounds.Height), selected ? Theme.Blue : Theme.Muted, TextFormatFlags.VerticalCenter | TextFormatFlags.Right);
        }

        private void LoadCatalogs()
        {
            projectOptions = new List<string>();
            languageOptions = new List<string>();
            languageVoiceRules = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            string projectFile = Path.Combine(projectRoot, "data", "projects.csv");
            string languageFile = Path.Combine(projectRoot, "data", "languages.csv");
            string voiceFile = Path.Combine(projectRoot, "data", "language_voice_rules.csv");

            if (File.Exists(projectFile)) {
                List<string[]> rows = ParseCsv(File.ReadAllText(projectFile, Encoding.UTF8));
                for (int i = 1; i < rows.Count; i++) {
                    string project = rows[i].Length > 0 ? rows[i][0].Trim().TrimStart('\uFEFF') : "";
                    if (project.Length > 0 && !projectOptions.Contains(project)) projectOptions.Add(project);
                }
            }
            if (File.Exists(languageFile)) {
                List<string[]> rows = ParseCsv(File.ReadAllText(languageFile, Encoding.UTF8));
                for (int i = 1; i < rows.Count; i++) {
                    string name = rows[i].Length > 0 ? rows[i][0].Trim().TrimStart('\uFEFF') : "";
                    if (name.Length > 0 && !languageOptions.Contains(name)) languageOptions.Add(name);
                }
            }
            if (File.Exists(voiceFile)) {
                List<string[]> rows = ParseCsv(File.ReadAllText(voiceFile, Encoding.UTF8));
                for (int i = 1; i < rows.Count; i++) {
                    string language = rows[i].Length > 0 ? rows[i][0].Trim().TrimStart('\uFEFF') : "";
                    string voice = rows[i].Length > 1 ? rows[i][1].Trim() : "";
                    if (language.Length > 0 && voice.Length > 0) languageVoiceRules[language] = voice;
                }
            }
        }

        private void OpenCatalogManager()
        {
            using (var dialog = new CatalogDialog(projectOptions, languageOptions)) {
                if (dialog.ShowDialog(this) != DialogResult.OK) return;
                projectOptions = dialog.Projects;
                languageOptions = dialog.Languages;
                SaveCatalogs();
                RefreshTaskComboCells();
                RefreshProjectList();
                AppendLog("项目与语言设置已更新。\r\n");
            }
        }

        private void SaveCatalogs()
        {
            string dataDir = Path.Combine(projectRoot, "data");
            Directory.CreateDirectory(dataDir);
            var projectLines = new List<string> { "name" };
            foreach (string project in projectOptions) projectLines.Add(CsvEscape(project));
            var languageLines = new List<string> { "name" };
            foreach (string language in languageOptions) languageLines.Add(CsvEscape(language));
            File.WriteAllText(Path.Combine(dataDir, "projects.csv"), String.Join("\r\n", projectLines.ToArray()) + "\r\n", new UTF8Encoding(true));
            File.WriteAllText(Path.Combine(dataDir, "languages.csv"), String.Join("\r\n", languageLines.ToArray()) + "\r\n", new UTF8Encoding(true));
        }

        private void ConfigureProjectOutputDirectories()
        {
            string configFile = Path.Combine(projectRoot, "data", "project_output_dirs.csv");
            var existing = new Dictionary<string, OutputDirectoryRule>(StringComparer.OrdinalIgnoreCase);
            var materialRules = new List<OutputDirectoryRule>();
            if (File.Exists(configFile)) {
                List<string[]> rows = ParseCsv(File.ReadAllText(configFile, Encoding.UTF8));
                string[] header = rows.Count > 0 ? rows[0] : new string[0];
                int projectColumn = CsvColumnIndex(header, "project");
                int materialColumn = CsvColumnIndex(header, "material");
                int baseColumn = CsvColumnIndex(header, "baseDir");
                int folderColumn = CsvColumnIndex(header, "folderName");
                int modeColumn = CsvColumnIndex(header, "dateMode");
                int valueColumn = CsvColumnIndex(header, "dateValue");
                int outputColumn = CsvColumnIndex(header, "outputDir");
                for (int i = 1; i < rows.Count; i++) {
                    string project = CsvCell(rows[i], projectColumn).Trim().TrimStart('\uFEFF');
                    if (project.Length == 0) continue;
                    var parsedRule = new OutputDirectoryRule {
                        Project = project,
                        Material = CsvCell(rows[i], materialColumn).Trim(),
                        BaseDir = CsvCell(rows[i], baseColumn).Trim(),
                        FolderName = CsvCell(rows[i], folderColumn).Trim(),
                        DateMode = CsvCell(rows[i], modeColumn).Trim().ToLowerInvariant(),
                        DateValue = CsvCell(rows[i], valueColumn).Trim(),
                        LegacyOutputDir = CsvCell(rows[i], outputColumn).Trim()
                    };
                    if (parsedRule.Material.Length > 0) materialRules.Add(parsedRule);
                    else existing[project] = parsedRule;
                }
            }

            var projects = new List<string>(projectOptions);
            if (projects.Count == 0) projects.AddRange(new[] { "ProjectA", "ProjectB", "ProjectC", "SPC地板", "SPC墙板" });
            var rules = new List<OutputDirectoryRule>();
            foreach (string project in projects) {
                OutputDirectoryRule rule;
                if (!existing.TryGetValue(project, out rule)) {
                    rule = new OutputDirectoryRule { Project = project, Material = "", BaseDir = "", FolderName = project + "音频", DateMode = "auto", DateValue = "", LegacyOutputDir = "" };
                } else if (rule.BaseDir.Length == 0 && rule.LegacyOutputDir.Length > 0) {
                    rule.BaseDir = rule.LegacyOutputDir;
                    rule.FolderName = "";
                    rule.DateMode = "none";
                } else {
                    if (rule.FolderName.Length == 0) rule.FolderName = project + "音频";
                    if (rule.DateMode != "auto" && rule.DateMode != "manual" && rule.DateMode != "none") rule.DateMode = "auto";
                }
                rules.Add(rule);
            }

            using (var dialog = new OutputDirectoryDialog(rules)) {
                if (dialog.ShowDialog(this) != DialogResult.OK) return;
                rules = dialog.Rules;
            }
            Directory.CreateDirectory(Path.GetDirectoryName(configFile));
            var lines = new List<string> { "project,material,baseDir,folderName,dateMode,dateValue,outputDir" };
            foreach (OutputDirectoryRule rule in rules) {
                lines.Add(String.Join(",", new[] { CsvEscape(rule.Project), "", CsvEscape(rule.BaseDir), CsvEscape(rule.FolderName), CsvEscape(rule.DateMode), CsvEscape(rule.DateValue), "" }));
            }
            foreach (OutputDirectoryRule rule in materialRules) {
                lines.Add(String.Join(",", new[] { CsvEscape(rule.Project), CsvEscape(rule.Material), CsvEscape(rule.BaseDir), CsvEscape(rule.FolderName), CsvEscape(rule.DateMode), CsvEscape(rule.DateValue), CsvEscape(rule.LegacyOutputDir) }));
            }
            File.WriteAllText(configFile, String.Join("\r\n", lines.ToArray()) + "\r\n", new UTF8Encoding(true));
            AppendLog("项目输出目录已更新。\r\n");
            MessageBox.Show("输出目录名称与日期规则已保存。\r\n自动日期会在每次开始任务时使用当天日期。", "保存成功", MessageBoxButtons.OK, MessageBoxIcon.Information);
        }

        private static int CsvColumnIndex(string[] header, string name)
        {
            for (int i = 0; i < header.Length; i++) if (header[i].Trim().TrimStart('\uFEFF').Equals(name, StringComparison.OrdinalIgnoreCase)) return i;
            return -1;
        }

        private static string CsvCell(string[] row, int index)
        {
            return index >= 0 && index < row.Length ? row[index] : "";
        }

        private void RefreshTaskComboCells()
        {
            if (taskGrid == null) return;
            foreach (DataGridViewRow row in taskGrid.Rows) ConfigureTaskRow(row);
        }

        private void ConfigureTaskRow(DataGridViewRow row)
        {
            if (row == null) return;
            updatingTaskCombos = true;
            try {
                string project = Convert.ToString(row.Cells["project"].Value);
                string language = Convert.ToString(row.Cells["language"].Value);
                string material = Convert.ToString(row.Cells["material"].Value);
                string voice = Convert.ToString(row.Cells["voice"].Value).Trim();
                if (voice.Length == 0 || voice.Equals("default", StringComparison.OrdinalIgnoreCase)) row.Cells["voice"].Value = VoiceForLanguage(language);

                var projectCell = new DataGridViewComboBoxCell { FlatStyle = FlatStyle.Flat, DisplayStyle = DataGridViewComboBoxDisplayStyle.DropDownButton };
                foreach (string item in projectOptions) projectCell.Items.Add(item);
                if (project.Length > 0 && !projectCell.Items.Contains(project)) projectCell.Items.Add(project);
                row.Cells["project"] = projectCell;
                projectCell.Value = project;

                var languageCell = new DataGridViewComboBoxCell { FlatStyle = FlatStyle.Flat, DisplayStyle = DataGridViewComboBoxDisplayStyle.DropDownButton };
                foreach (string item in languageOptions) languageCell.Items.Add(item);
                if (language.Length > 0 && !languageCell.Items.Contains(language)) languageCell.Items.Add(language);
                row.Cells["language"] = languageCell;
                languageCell.Value = language;

                var materialCell = new DataGridViewComboBoxCell { FlatStyle = FlatStyle.Flat, DisplayStyle = DataGridViewComboBoxDisplayStyle.DropDownButton };
                foreach (string item in MaterialOptionsForProject(project)) materialCell.Items.Add(item);
                string selectedMaterial = String.IsNullOrWhiteSpace(material) ? "自动随机" : material;
                if (!materialCell.Items.Contains(selectedMaterial)) selectedMaterial = materialCell.Items.Count > 0 ? Convert.ToString(materialCell.Items[0]) : "自动随机";
                row.Cells["material"] = materialCell;
                materialCell.Value = selectedMaterial;
            } finally { updatingTaskCombos = false; }
        }

        private static string[] MaterialOptionsForProject(string project)
        {
            if (String.Equals(project, "SPC地板", StringComparison.OrdinalIgnoreCase)) return new[] { "塑木地板" };
            if (String.Equals(project, "SPC墙板", StringComparison.OrdinalIgnoreCase)) return new[] { "晶岩墙板" };
            return new[] { "自动随机", "净白板", "碳岩板", "金属板", "镜面板", "木饰面", "PU石", "PU软石" };
        }

        private string VoiceForLanguage(string language)
        {
            string voice;
            return languageVoiceRules.TryGetValue(language ?? "", out voice) && voice.Length > 0 ? voice : "default";
        }

        private void OnTaskCellValueChanged(object sender, DataGridViewCellEventArgs e)
        {
            if (updatingTaskCombos || e.RowIndex < 0 || e.ColumnIndex < 0) return;
            string columnName = taskGrid.Columns[e.ColumnIndex].Name;
            DataGridViewRow changedRow = taskGrid.Rows[e.RowIndex];
            bool refreshPreview = false;
            if (columnName == "project") {
                ConfigureTaskRow(changedRow);
                refreshPreview = ClearAutomaticPreview(changedRow);
            }
            if (columnName == "language") {
                changedRow.Cells["voice"].Value = VoiceForLanguage(Convert.ToString(changedRow.Cells["language"].Value));
                refreshPreview = ClearAutomaticPreview(changedRow);
            }
            if (columnName == "material") {
                refreshPreview = ClearAutomaticPreview(changedRow);
                RefreshUnifiedMaterialSelector();
            }
            if (refreshPreview && !suppressImmediatePreview) QueueImmediatePreview(changedRow);
            if (!applyingTaskPreview && columnName == "text") {
                previewRows.Remove(changedRow);
                changedRow.Cells["materialPreview"].Value = "";
                changedRow.Cells["text"].ToolTipText = "";
            }
        }

        private bool ClearAutomaticPreview(DataGridViewRow row)
        {
            bool wasPreview = previewRows.Remove(row);
            string currentText = Convert.ToString(row.Cells["text"].Value);
            if (!wasPreview && !String.IsNullOrWhiteSpace(currentText)) return false;
            applyingTaskPreview = true;
            try {
                row.Cells["text"].Value = "";
                row.Cells["materialPreview"].Value = "";
                row.Cells["text"].ToolTipText = "";
            } finally { applyingTaskPreview = false; }
            return true;
        }

        private void QueueImmediatePreview(DataGridViewRow row)
        {
            if (row == null || pendingPreviewRows.Contains(row)) return;
            pendingPreviewRows.Add(row);
            BeginInvoke(new Action(delegate {
                pendingPreviewRows.Remove(row);
                if (suppressImmediatePreview || row.DataGridView != taskGrid) return;
                ApplyRandomPreviewForRow(row);
            }));
        }

        private void ApplyRandomPreviewForRow(DataGridViewRow row)
        {
            if (row == null || row.DataGridView != taskGrid) return;
            if (!String.IsNullOrWhiteSpace(Convert.ToString(row.Cells["text"].Value)) && !previewRows.Contains(row)) return;
            string script = Path.Combine(projectRoot, "src", "previewCopies.js");
            if (!File.Exists(script)) return;
            string payload = String.Join("\u001f", new[] {
                Convert.ToString(row.Cells["id"].Value),
                Convert.ToString(row.Cells["project"].Value),
                Convert.ToString(row.Cells["language"].Value),
                Convert.ToString(row.Cells["material"].Value)
            });
            string encodedTask = Convert.ToBase64String(Encoding.UTF8.GetBytes(payload));
            try {
                var info = new ProcessStartInfo {
                    FileName = FindNode(),
                    Arguments = "\"" + script + "\" --task-base64 \"" + encodedTask + "\"",
                    WorkingDirectory = projectRoot,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                    StandardOutputEncoding = Encoding.UTF8,
                    StandardErrorEncoding = Encoding.UTF8
                };
                string output;
                string error;
                using (Process process = Process.Start(info)) {
                    output = process.StandardOutput.ReadToEnd();
                    error = process.StandardError.ReadToEnd();
                    process.WaitForExit();
                    if (process.ExitCode != 0) throw new InvalidOperationException(error.Trim());
                }

                string[] outputLines = output.Split(new[] { "\r\n", "\n" }, StringSplitOptions.RemoveEmptyEntries);
                string line = outputLines.Length > 0 ? outputLines[0] : "";
                if (String.IsNullOrWhiteSpace(line)) throw new InvalidOperationException("没有返回随机文案预览");
                string[] fields = line.Split('\t');
                if (fields.Length < 4) throw new InvalidOperationException("随机文案预览格式无效");
                string rowId = DecodeBase64(fields[0]);
                if (!String.Equals(rowId, Convert.ToString(row.Cells["id"].Value), StringComparison.OrdinalIgnoreCase)) return;

                applyingTaskPreview = true;
                try {
                    row.Cells["text"].Value = DecodeBase64(fields[1]);
                    row.Cells["materialPreview"].Value = DecodeBase64(fields[2]);
                    row.Cells["text"].ToolTipText = DecodeBase64(fields[2]) + " · " + DecodeBase64(fields[3]) + "（随机预览，正式开始任务时才记录使用）";
                    previewRows.Add(row);
                } finally { applyingTaskPreview = false; }
            } catch (Exception ex) {
                AppendLog("即时文案预览失败：" + ex.Message + "\r\n");
            }
        }

        private void OnTaskCellFormatting(object sender, DataGridViewCellFormattingEventArgs e)
        {
            if (e.RowIndex < 0 || e.ColumnIndex < 0) return;
            if (taskGrid.Columns[e.ColumnIndex].Name != "materialPreview") return;
            if (String.IsNullOrWhiteSpace(Convert.ToString(e.Value))) return;
            e.CellStyle.ForeColor = Theme.Blue;
            e.CellStyle.SelectionForeColor = Theme.Blue;
        }

        private string FirstProject()
        {
            return projectOptions.Count > 0 ? projectOptions[0] : "";
        }

        private void AddTaskRow()
        {
            int number = taskGrid.Rows.Count + 1;
            string project = selectedProject.Length > 0 ? selectedProject : FirstProject();
            if (project.Length == 0) {
                MessageBox.Show("请先在“管理项目/语言”中添加项目。", "没有项目", MessageBoxButtons.OK, MessageBoxIcon.Information);
                return;
            }
            string language = languageOptions.Count > 0 ? languageOptions[0] : "";
            int index = taskGrid.Rows.Add(true, number.ToString("000"), project, language, "自动随机", "", VoiceForLanguage(language), "", "", "");
            ConfigureTaskRow(taskGrid.Rows[index]);
            taskGrid.Rows[index].Visible = true;
            taskGrid.ClearSelection();
            taskGrid.Rows[index].Selected = true;
            taskGrid.CurrentCell = taskGrid.Rows[index].Cells["language"];
            taskGrid.BeginEdit(true);
            UpdateTaskPageCount();
            projectList.Invalidate();
        }

        private void DeleteSelectedTasks()
        {
            var indexes = new List<int>();
            foreach (DataGridViewRow row in taskGrid.SelectedRows) if (!indexes.Contains(row.Index)) indexes.Add(row.Index);
            if (indexes.Count == 0 && taskGrid.CurrentRow != null) indexes.Add(taskGrid.CurrentRow.Index);
            if (indexes.Count == 0) return;
            DialogResult answer = MessageBox.Show("从任务列表中移除选中的 " + indexes.Count + " 条任务？\r\n保存前可以通过“重新加载”恢复。", "删除任务", MessageBoxButtons.YesNo, MessageBoxIcon.Question);
            if (answer != DialogResult.Yes) return;
            indexes.Sort(); indexes.Reverse();
            foreach (int index in indexes) if (index >= 0 && index < taskGrid.Rows.Count) taskGrid.Rows.RemoveAt(index);
            UpdateTaskPageCount();
            projectList.Invalidate();
        }

        private void ImportTasks()
        {
            using (var dialog = new OpenFileDialog { Filter = "CSV 任务表 (*.csv)|*.csv|所有文件 (*.*)|*.*", Title = "导入任务表" }) {
                if (dialog.ShowDialog(this) != DialogResult.OK) return;
                try { LoadTaskFile(dialog.FileName); AppendLog("已导入任务表：" + dialog.FileName + "\r\n"); }
                catch (Exception ex) { MessageBox.Show(ex.Message, "导入失败", MessageBoxButtons.OK, MessageBoxIcon.Error); }
            }
        }

        private void LoadTasksIntoGrid()
        {
            try { LoadTaskFile(CurrentTaskFile()); }
            catch (Exception ex) { MessageBox.Show(ex.Message, "读取任务失败", MessageBoxButtons.OK, MessageBoxIcon.Error); }
        }

        private void LoadTaskFile(string file)
        {
            taskGrid.Rows.Clear();
            previewRows.Clear();
            if (!File.Exists(file)) { RefreshProjectList(); UpdateTaskPageCount(); return; }
            List<string[]> records = ParseCsv(File.ReadAllText(file, Encoding.UTF8));
            if (records.Count == 0) { RefreshProjectList(); UpdateTaskPageCount(); return; }
            var columns = new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase);
            for (int i = 0; i < records[0].Length; i++) columns[records[0][i].Trim().TrimStart('\uFEFF')] = i;
            string[] names = { "id", "project", "language", "voice", "text", "outputName", "status" };
            for (int rowIndex = 1; rowIndex < records.Count; rowIndex++) {
                string[] source = records[rowIndex];
                if (source.Length == 1 && String.IsNullOrWhiteSpace(source[0])) continue;
                var values = new string[names.Length];
                for (int i = 0; i < names.Length; i++) {
                    int sourceIndex;
                    values[i] = columns.TryGetValue(names[i], out sourceIndex) && sourceIndex < source.Length ? source[sourceIndex] : "";
                }
                if (values[1].Length == 0) {
                    int legacyIndex;
                    if (columns.TryGetValue("majorCategory", out legacyIndex) && legacyIndex < source.Length) values[1] = source[legacyIndex];
                    else if (columns.TryGetValue("category", out legacyIndex) && legacyIndex < source.Length) values[1] = source[legacyIndex];
                }
                string selectedMaterial = "";
                int materialIndex;
                if (columns.TryGetValue("material", out materialIndex) && materialIndex < source.Length) selectedMaterial = source[materialIndex];
                bool selectedForRun = !String.Equals(values[6].Trim(), "done", StringComparison.OrdinalIgnoreCase);
                int newRow = taskGrid.Rows.Add(selectedForRun, values[0], values[1], values[2], selectedMaterial, "", values[3], values[4], values[5], values[6]);
                ConfigureTaskRow(taskGrid.Rows[newRow]);
            }
            if (String.Equals(Path.GetFullPath(file), Path.GetFullPath(CurrentTaskFile()), StringComparison.OrdinalIgnoreCase)) {
                ApplyRandomPreviews();
            }
            RefreshProjectList();
            UpdateTaskPageCount();
        }

        private void SaveTasksFromGrid()
        {
            try {
                taskGrid.EndEdit();
                var lines = new List<string> { "id,project,language,material,voice,text,outputName,status" };
                foreach (DataGridViewRow row in taskGrid.Rows) {
                    var fields = new List<string>();
                    foreach (string name in new[] { "id", "project", "language", "material", "voice", "text", "outputName", "status" }) {
                        object value = row.Cells[name].Value;
                        if (name == "text" && previewRows.Contains(row)) value = "";
                        if (name == "material" && String.Equals(Convert.ToString(value), "自动随机", StringComparison.OrdinalIgnoreCase)) value = "";
                        fields.Add(CsvEscape(value == null ? "" : value.ToString()));
                    }
                    lines.Add(String.Join(",", fields.ToArray()));
                }
                string file = CurrentTaskFile();
                Directory.CreateDirectory(Path.GetDirectoryName(file));
                File.WriteAllText(file, String.Join("\r\n", lines.ToArray()) + "\r\n", new UTF8Encoding(true));
                applyingTaskPreview = true;
                try {
                    foreach (DataGridViewRow row in previewRows) { row.Cells["text"].Value = ""; row.Cells["materialPreview"].Value = ""; }
                    previewRows.Clear();
                } finally { applyingTaskPreview = false; }
                ApplyRandomPreviews();
                taskPageCount.Text = selectedProject.Length == 0 ? taskGrid.Rows.Count + " 条任务 · 已保存" : selectedProject + " · " + ProjectTaskCount(selectedProject) + " 条文案 · 已保存";
                AppendLog("任务表已保存，共 " + taskGrid.Rows.Count + " 条任务。\r\n");
                RefreshSummary();
            } catch (Exception ex) { MessageBox.Show(ex.Message, "保存失败", MessageBoxButtons.OK, MessageBoxIcon.Error); }
        }

        private void UpdateTaskPageCount()
        {
            if (taskPageCount == null || taskGrid == null) return;
            taskPageCount.Text = selectedProject.Length == 0 ? taskGrid.Rows.Count + " 条任务" : selectedProject + " · " + ProjectTaskCount(selectedProject) + " 条文案";
        }

        private string CurrentTaskFile()
        {
            return ResolveProjectPath(ReadEnvValue(Path.Combine(projectRoot, ".env"), "TASK_FILE", "data/tasks.csv"));
        }

        private void ApplyRandomPreviews()
        {
            string script = Path.Combine(projectRoot, "src", "previewCopies.js");
            if (!File.Exists(script)) return;
            try {
                var info = new ProcessStartInfo {
                    FileName = FindNode(),
                    Arguments = "\"" + script + "\"",
                    WorkingDirectory = projectRoot,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                    StandardOutputEncoding = Encoding.UTF8,
                    StandardErrorEncoding = Encoding.UTF8
                };
                string output;
                string error;
                using (Process process = Process.Start(info)) {
                    output = process.StandardOutput.ReadToEnd();
                    error = process.StandardError.ReadToEnd();
                    process.WaitForExit();
                    if (process.ExitCode != 0) throw new InvalidOperationException(error.Trim());
                }

                var previews = new Dictionary<string, string[]>(StringComparer.OrdinalIgnoreCase);
                foreach (string line in output.Split(new[] { "\r\n", "\n" }, StringSplitOptions.RemoveEmptyEntries)) {
                    string[] fields = line.Split('\t');
                    if (fields.Length < 4) continue;
                    string id = DecodeBase64(fields[0]);
                    previews[id] = new[] { DecodeBase64(fields[1]), DecodeBase64(fields[2]), DecodeBase64(fields[3]) };
                }

                applyingTaskPreview = true;
                try {
                    foreach (DataGridViewRow row in taskGrid.Rows) {
                        string id = Convert.ToString(row.Cells["id"].Value);
                        string currentText = Convert.ToString(row.Cells["text"].Value);
                        string[] preview;
                        if (currentText.Length > 0 || !previews.TryGetValue(id, out preview)) continue;
                        row.Cells["text"].Value = preview[0];
                        row.Cells["materialPreview"].Value = preview[1];
                        row.Cells["text"].ToolTipText = preview[1] + " · " + preview[2] + "（随机预览，保存时保持自动随机）";
                        previewRows.Add(row);
                    }
                } finally { applyingTaskPreview = false; }
            } catch (Exception ex) {
                AppendLog("随机文案预览失败：" + ex.Message + "\r\n");
            }
        }

        private static string DecodeBase64(string value)
        {
            return Encoding.UTF8.GetString(Convert.FromBase64String(value));
        }

        private static string CsvEscape(string value)
        {
            if (value.IndexOfAny(new[] { ',', '"', '\r', '\n' }) < 0) return value;
            return "\"" + value.Replace("\"", "\"\"") + "\"";
        }

        private static List<string[]> ParseCsv(string content)
        {
            var records = new List<string[]>();
            var row = new List<string>();
            var field = new StringBuilder();
            bool quoted = false;
            for (int i = 0; i < content.Length; i++) {
                char c = content[i];
                if (quoted) {
                    if (c == '"' && i + 1 < content.Length && content[i + 1] == '"') { field.Append('"'); i++; }
                    else if (c == '"') quoted = false;
                    else field.Append(c);
                } else if (c == '"') quoted = true;
                else if (c == ',') { row.Add(field.ToString()); field.Clear(); }
                else if (c == '\n') { row.Add(field.ToString().TrimEnd('\r')); field.Clear(); records.Add(row.ToArray()); row = new List<string>(); }
                else field.Append(c);
            }
            if (field.Length > 0 || row.Count > 0) { row.Add(field.ToString().TrimEnd('\r')); records.Add(row.ToArray()); }
            return records;
        }

        private void RefreshSummary()
        {
            string envPath = Path.Combine(projectRoot, ".env");
            string target = ReadEnvValue(envPath, "TARGET_URL", "https://example.com/voice");
            string taskPath = ResolveProjectPath(ReadEnvValue(envPath, "TASK_FILE", "data/tasks.csv"));
            totalTasks = CountTasks(taskPath);
            bool ready = File.Exists(Path.Combine(projectRoot, ".browsers", "chrome-win64", "chrome.exe"));
            bool headless = ReadEnvValue(envPath, "HEADLESS", "false").Equals("true", StringComparison.OrdinalIgnoreCase);
            targetValue.Text = target;
            taskValue.Text = totalTasks + " 条任务";
            browserValue.Text = ready ? "已安装" : "未安装";
            modeValue.Text = headless ? "后台模式" : "可视化模式";
            sideBrowser.Text = "浏览器    " + (ready ? "已安装" : "未安装");
            sideVersion.Text = "版本        " + BrowserVersion();
            UpdateProgress();
        }

        private void StartGeneration()
        {
            if (currentJob != null && !currentJob.HasExited) return;
            List<string> runProjects = ChooseProjectsForRun();
            if (runProjects == null) return;
            List<string> runTaskIds = SelectedTaskIds(runProjects);
            if (runTaskIds.Count == 0) {
                MessageBox.Show("请至少勾选一条需要生成的任务。", "没有勾选任务", MessageBoxButtons.OK, MessageBoxIcon.Information);
                return;
            }
            string script = Path.Combine(projectRoot, "src", "runBatch.js");
            string browser = ResolveProjectPath(ReadEnvValue(Path.Combine(projectRoot, ".env"), "BROWSER_EXECUTABLE", ".browsers\\chrome-win64\\chrome.exe"));
            if (!File.Exists(script) || !File.Exists(browser)) { MessageBox.Show("运行文件或 Chromium 不完整，请检查项目目录。", "无法启动", MessageBoxButtons.OK, MessageBoxIcon.Error); return; }

            completedTasks = failedTasks = processingTasks = 0;
            RefreshSummary();
            totalTasks = runTaskIds.Count;
            taskValue.Text = totalTasks + " 条任务";
            UpdateProgress();
            logBox.Clear();
            AppendLog("本次项目：" + String.Join("、", runProjects.ToArray()) + "\r\n");
            AppendLog("正在启动 Playwright 批量任务...\r\n");
            var info = new ProcessStartInfo { FileName = FindNode(), Arguments = "\"" + script + "\"", WorkingDirectory = projectRoot, UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true, StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8 };
            info.EnvironmentVariables["RUN_PROJECTS"] = String.Join("|", runProjects.ToArray());
            info.EnvironmentVariables["RUN_TASK_IDS"] = String.Join("|", runTaskIds.ToArray());
            try {
                currentJob = new Process { StartInfo = info, EnableRaisingEvents = true };
                currentJob.OutputDataReceived += delegate(object sender, DataReceivedEventArgs e) { if (e.Data != null) HandleOutput(e.Data, false); };
                currentJob.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs e) { if (e.Data != null) HandleOutput(e.Data, true); };
                currentJob.Exited += delegate { JobFinished(); };
                currentJob.Start(); currentJob.BeginOutputReadLine(); currentJob.BeginErrorReadLine();
                runStatus.Text = "●  正在运行"; runStatus.ForeColor = Theme.Blue; startButton.Enabled = false; stopButton.Enabled = true;
            } catch (Exception ex) { AppendLog("启动失败：" + ex.Message + "\r\n"); MessageBox.Show(ex.Message, "启动失败", MessageBoxButtons.OK, MessageBoxIcon.Error); }
        }

        private List<string> ChooseProjectsForRun()
        {
            var projects = new List<string>();
            if (projectList != null) {
                foreach (object item in projectList.Items) {
                    string project = Convert.ToString(item);
                    if (ProjectHasRunnableTasks(project)) projects.Add(project);
                }
            }
            if (projects.Count == 0) {
                MessageBox.Show("任务表中没有可执行任务。", "没有任务", MessageBoxButtons.OK, MessageBoxIcon.Information);
                return null;
            }
            using (var dialog = new ProjectRunDialog(projects)) {
                return dialog.ShowDialog(this) == DialogResult.OK ? dialog.SelectedProjects : null;
            }
        }

        private bool ProjectHasRunnableTasks(string project)
        {
            foreach (DataGridViewRow row in taskGrid.Rows) {
                if (!String.Equals(Convert.ToString(row.Cells["project"].Value), project, StringComparison.OrdinalIgnoreCase)) continue;
                if (TaskChecked(row) && !String.Equals(Convert.ToString(row.Cells["status"].Value).Trim(), "done", StringComparison.OrdinalIgnoreCase)) return true;
            }
            return false;
        }

        private List<string> SelectedTaskIds(List<string> projects)
        {
            var ids = new List<string>();
            foreach (DataGridViewRow row in taskGrid.Rows) {
                if (!projects.Contains(Convert.ToString(row.Cells["project"].Value))) continue;
                if (!TaskChecked(row)) continue;
                if (String.Equals(Convert.ToString(row.Cells["status"].Value).Trim(), "done", StringComparison.OrdinalIgnoreCase)) continue;
                string id = Convert.ToString(row.Cells["id"].Value).Trim();
                if (id.Length > 0 && !ids.Contains(id)) ids.Add(id);
            }
            return ids;
        }

        private static bool TaskChecked(DataGridViewRow row)
        {
            object value = row.Cells["runSelected"].Value;
            return value != null && value != DBNull.Value && Convert.ToBoolean(value);
        }

        private int CountRunnableTasks(List<string> projects)
        {
            return SelectedTaskIds(projects).Count;
        }

        private void HandleOutput(string line, bool error)
        {
            if (IsDisposed) return;
            BeginInvoke((MethodInvoker)delegate {
                AppendLog("[" + DateTime.Now.ToString("HH:mm:ss") + "] " + (error ? "[错误] " : "") + line + "\r\n");
                string lower = line.ToLowerInvariant();
                if (lower.Contains("task started")) processingTasks = 1;
                if (lower.Contains("task finished")) { completedTasks++; processingTasks = 0; }
                if (lower.Contains("task failed")) { failedTasks++; processingTasks = 0; }
                UpdateProgress();
            });
        }

        private void UpdateProgress()
        {
            int done = completedTasks + failedTasks;
            int pending = Math.Max(0, totalTasks - done - processingTasks);
            progressRing.Percentage = totalTasks == 0 ? 0 : (int)Math.Round(done * 100D / totalTasks);
            completedValue.Text = completedTasks.ToString(); processingValue.Text = processingTasks.ToString(); failedValue.Text = failedTasks.ToString(); pendingValue.Text = pending.ToString();
        }

        private void StopGeneration()
        {
            try { if (currentJob != null && !currentJob.HasExited) { currentJob.Kill(); AppendLog("已请求停止当前任务。\r\n"); } }
            catch (Exception ex) { AppendLog("停止失败：" + ex.Message + "\r\n"); }
        }

        private void JobFinished()
        {
            if (IsDisposed) return;
            BeginInvoke((MethodInvoker)delegate {
                int code = currentJob == null ? -1 : currentJob.ExitCode;
                runStatus.Text = code == 0 ? "●  运行成功" : "●  已停止";
                runStatus.ForeColor = code == 0 ? Theme.Green : Theme.Red;
                startButton.Enabled = true; stopButton.Enabled = false;
                AppendLog("[" + DateTime.Now.ToString("HH:mm:ss") + "] 任务结束，退出码：" + code + "\r\n");
                UpdateProgress();
            });
        }

        private void OpenControlPanel()
        {
            try {
                if (!IsControlPanelOnline()) {
                    var info = new ProcessStartInfo { FileName = FindNode(), Arguments = "\"" + Path.Combine(projectRoot, "src", "clientServer.js") + "\"", WorkingDirectory = projectRoot, UseShellExecute = false, CreateNoWindow = true };
                    controlServer = Process.Start(info); System.Threading.Thread.Sleep(650);
                }
                Process.Start(new ProcessStartInfo("http://127.0.0.1:5177") { UseShellExecute = true });
            } catch (Exception ex) { MessageBox.Show(ex.Message, "无法打开控制台", MessageBoxButtons.OK, MessageBoxIcon.Error); }
        }

        private bool IsControlPanelOnline()
        {
            try { var request = WebRequest.Create("http://127.0.0.1:5177/api/job"); request.Timeout = 350; using (request.GetResponse()) return true; }
            catch { return false; }
        }

        private void OpenDownloads()
        {
            string directory = ResolveProjectPath(ReadEnvValue(Path.Combine(projectRoot, ".env"), "DOWNLOAD_DIR", "downloads"));
            Directory.CreateDirectory(directory);
            Process.Start(new ProcessStartInfo("explorer.exe", "\"" + directory + "\"") { UseShellExecute = true });
        }

        private void SaveLog()
        {
            using (var dialog = new SaveFileDialog { Filter = "日志文件 (*.log)|*.log|文本文件 (*.txt)|*.txt", FileName = "batch-voice-" + DateTime.Now.ToString("yyyyMMdd-HHmmss") + ".log" }) {
                if (dialog.ShowDialog(this) == DialogResult.OK) File.WriteAllText(dialog.FileName, logBox.Text, Encoding.UTF8);
            }
        }

        private void OpenHelp()
        {
            string file = Path.Combine(projectRoot, "桌面客户端说明.md");
            if (File.Exists(file)) Process.Start(new ProcessStartInfo(file) { UseShellExecute = true });
        }

        private void ShowAbout()
        {
            MessageBox.Show("批量语音客户端 v1.0.0\r\n\r\n基于 Playwright 的自动化批量语音生成工具。", "关于", MessageBoxButtons.OK, MessageBoxIcon.Information);
        }

        private string BrowserVersion()
        {
            try {
                string file = ResolveProjectPath(ReadEnvValue(Path.Combine(projectRoot, ".env"), "BROWSER_EXECUTABLE", ".browsers\\chrome-win64\\chrome.exe"));
                return File.Exists(file) ? FileVersionInfo.GetVersionInfo(file).ProductVersion : "未安装";
            } catch { return "浏览器"; }
        }

        private string ResolveProjectPath(string value) { return Path.IsPathRooted(value) ? value : Path.GetFullPath(Path.Combine(projectRoot, value.Replace('/', Path.DirectorySeparatorChar))); }
        private static string ReadEnvValue(string file, string key, string fallback) { if (!File.Exists(file)) return fallback; foreach (string raw in File.ReadAllLines(file, Encoding.UTF8)) { string line = raw.Trim(); if (line.StartsWith(key + "=", StringComparison.OrdinalIgnoreCase)) return line.Substring(key.Length + 1).Trim(); } return fallback; }
        private static int CountTasks(string file) { if (!File.Exists(file)) return 0; int count = 0; foreach (string line in File.ReadAllLines(file, Encoding.UTF8)) if (!String.IsNullOrWhiteSpace(line)) count++; return Math.Max(0, count - 1); }
        private string FindNode()
        {
            string bundled = Path.Combine(projectRoot, "runtime", "node.exe");
            if (File.Exists(bundled)) return bundled;
            string installed = @"C:\Program Files\nodejs\node.exe";
            return File.Exists(installed) ? installed : "node.exe";
        }

        private static Label MakeLabel(string text, float size, FontStyle style, Color color, int x, int y, int width, int height)
        {
            return new Label { Text = text, Font = new Font("Microsoft YaHei UI", size, style), ForeColor = color, BackColor = Color.Transparent, Location = new Point(x, y), Size = new Size(width, height), AutoEllipsis = true };
        }

        internal static Label CreateLabel(string text, float size, FontStyle style, Color color, int x, int y, int width, int height)
        {
            return MakeLabel(text, size, style, color, x, y, width, height);
        }

        private void AppendLog(string text)
        {
            if (logBox.InvokeRequired) { logBox.BeginInvoke((MethodInvoker)delegate { AppendLog(text); }); return; }
            logBox.AppendText(text); logBox.SelectionStart = logBox.TextLength; logBox.ScrollToCaret();
        }

        private void OnFormClosing(object sender, FormClosingEventArgs e)
        {
            if (currentJob != null && !currentJob.HasExited) {
                DialogResult result = MessageBox.Show("任务仍在运行，关闭客户端会停止任务。确定关闭吗？", "确认关闭", MessageBoxButtons.YesNo, MessageBoxIcon.Warning);
                if (result != DialogResult.Yes) { e.Cancel = true; return; }
                StopGeneration();
            }
        }

        [STAThread]
        public static void Main()
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new MainForm());
        }
    }
}
