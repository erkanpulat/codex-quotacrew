# QuotaCrew for Codex

Manage multiple Codex accounts, compare remaining quotas and continue verified
quota-interrupted conversations from one Windows application.

[English guide](https://github.com/erkanpulat/codex-quotacrew#readme) ·
[Türkçe rehber](https://github.com/erkanpulat/codex-quotacrew/blob/main/README.tr.md) ·
[Downloads](https://github.com/erkanpulat/codex-quotacrew/releases/latest) ·
[Security](https://github.com/erkanpulat/codex-quotacrew/blob/main/SECURITY.md)

## Get started

1. Open QuotaCrew. For the portable package, extract the entire ZIP and run
   `QuotaCrew.exe`; keep the `_internal` folder beside it.
2. Choose your preferences in the first-run assistant. Python is included. If
   Codex CLI is missing, the assistant can install OpenAI's official standalone
   CLI with your confirmation; Node.js is not required.
3. Add an account and complete Codex sign-in. Enable monitoring and choose your
   switching and continuation preferences when ready.

Existing Codex conversations are preserved. QuotaCrew stores its profiles and
settings locally; System check shows their locations. Upgrades and removal preserve
that data. On Windows, saved profile credentials and switch recovery snapshots
use current-user DPAPI encryption. The active shared Codex file and SQLite data
remain unencrypted with restricted access permissions. Conversation titles,
previews, workspace paths and saved local goal notes are cached in SQLite.
See Help → Privacy policy for retention and removal instructions.
Never share profile directories, `auth.json`, account databases or recovery files.

Desktop and IDE continuation are experimental. IDE continuation requires the
OpenAI Codex extension and the IDE continuation option in QuotaCrew. Automatic
VS Code refresh supports one local VS Code window. Save your work; connection and
approval prompts remain yours to answer. Controlled switching can take a few
minutes and does not increase account quotas.

Direct-download installed copies check for stable releases when automatic checks are enabled;
downloads and installation start when you choose **Update**. Only QuotaCrew
restarts. Portable copies are replaced manually from the downloads page: close
QuotaCrew, extract the new ZIP into a clean folder and run the new executable.
MSIX installations use Microsoft Store updates; GitHub EXE updates are disabled.

## Başlarken

1. QuotaCrew uygulamasını açın. Taşınabilir pakette ZIP'in tamamını çıkarıp
   `QuotaCrew.exe` dosyasını çalıştırın; `_internal` klasörünü yanında tutun.
2. İlk açılışta tercihlerinizi seçin. Python pakete dahildir. Codex CLI eksikse
   kurulum yardımcısı onayınızla OpenAI'ın resmi bağımsız CLI paketini kurabilir;
   Node.js gerekmez.
3. Hesap ekleyip Codex girişini tamamlayın. Hazır olduğunuzda izlemeyi açıp hesap
   geçişi ve konuşma devamı tercihlerinizi belirleyin.

Mevcut Codex konuşmaları korunur. QuotaCrew hesap profillerini ve ayarlarını
bilgisayarınızda saklar; konumlarını Sistem Kontrolü bölümünde görebilirsiniz.
Güncelleme ve kaldırma bu verileri korur. Windows'ta kayıtlı profil giriş bilgileri
ve geçiş kurtarma kayıtları kullanıcıya bağlı DPAPI ile şifrelenir. Etkin ortak
Codex dosyası ve SQLite verileri şifrelenmez; erişim izinleriyle korunur. Konuşma
başlıkları, önizlemeler, çalışma klasörü yolları ve yerel hedef notları SQLite'ta
saklanır. Saklama ve silme adımları için Yardım → Gizlilik politikası bölümüne bakın. Profil
klasörlerini, `auth.json` dosyasını, hesap veritabanını ve kurtarma dosyalarını
paylaşmayın.

Desktop ve IDE konuşma devamı deneyseldir. IDE devamı için OpenAI Codex eklentisi
ve QuotaCrew'deki IDE devamı seçeneği gerekir. Otomatik VS Code yenilemesi tek
yerel VS Code penceresini destekler. Çalışmanızı kaydedin; bağlantı ve onay
isteklerini siz yanıtlayın. Kontrollü hesap geçişi birkaç dakika sürebilir ve
hesap kotalarını artırmaz.

Doğrudan indirilen kurulu sürüm, otomatik kontrol açıksa kararlı sürümleri kontrol eder; indirme ve
kurulum **Güncelle** seçildiğinde başlar. Yalnızca QuotaCrew yeniden başlar.
Taşınabilir sürümü indirme sayfasından elle güncelleyin: QuotaCrew'ü kapatın,
yeni ZIP'i temiz bir klasöre çıkarın ve yeni uygulamayı açın. MSIX kurulumları
Microsoft Store'dan güncellenir; GitHub EXE güncellemeleri kapalıdır.

MIT-licensed independent community software; not affiliated with OpenAI.
Application and dependency licenses are included in this package.
