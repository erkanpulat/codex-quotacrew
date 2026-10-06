# Windows kabul testleri

Ayrı bir Windows kullanıcısı veya sanal makinede, size ait test hesaplarıyla çalışın. Sonuçları geçti/kaldı/denenmedi olarak kaydedin; kişisel veri veya giriş bilgilerini raporlara eklemeyin. Otomatik testler canlı Codex bağlantı testinin yerine geçmez.

| Alan | Kontrol |
| --- | --- |
| Kurulum | CLI var/yok durumlarında ilk açılış; kurulum onayı, iptal ve yeniden deneme. |
| Güncelleme | Önceki sürümden yükseltme; hesap, ayar ve sohbetlerin korunması. Bozuk indirme çalıştırılmamalı. |
| Kaldırma | Uygulama ve kısayollar kaldırılmalı; hesap verileri ve Codex geçmişi korunmalı. |
| Arayüz | Türkçe/İngilizce, açık/koyu tema, küçük pencere ve farklı Windows ölçekleri. Uzun metinler ve tablolar erişilebilir kalmalı. |
| Hesap tabloları | Aynı arama, filtre, sıralama ve gizleme sonuçları; yükleme sırasında içerik kaymaması. |
| Hesap girişi | Başka tarayıcıda bağlantı açma, iptal ve tekrar giriş; işlem bitmeden hesabın bağlı sayılmaması. |
| Hesap geçişi | İki hesap arasında geçiş; doğru aktif kimlik, güncel kota ve korunan geçmiş. |
| Bağlantı hatası | Ağ kesintisi, uyku/uyanma ve geçersiz oturum. Belirsiz veri kullanılabilir kota sayılmamalı. |
| Duraklatma | Otomatik geçiş ve devam durmalı; hesap bilgileri yenilenmeye devam etmeli. |
| İş takibi | Çalışan, tamamlanan, durdurulan ve silinen konuşmalar; eski işlerin çalışan sayısında kalmaması. |
| Desktop devamı | Gerçek kota kesintisinden sonra aynı konuşmaya tek devam isteği; hedef, izin ve bütçenin korunması. |
| VS Code devamı | Eklenti hesabını ayrıca doğrulayın. Tek pencereyle yenileme, aynı sohbetin açılması ve tek devam isteği. |
| Devam engelleri | Kullanıcı durdurması, onay bekleme, başka çalışan iş, birden çok IDE penceresi ve belirsiz teslimatta otomatik tekrar olmaması. |
| Otomatik kapatma | Süre, seçilen konuşma ve tüm 5 saatlik kotalar koşulları; doğrulama, iptal ve eski veride bekleme. |
| Tepsi ve başlangıç | Pencereyi gizleme/açma, Çıkış, Windows başlangıç tercihi ve tek uygulama örneği. |

Gerçek kapatma testini yalnızca ayrı test oturumunda, çalışmalar kaydedildikten sonra yapın. Otomatik testler kapatma işlemini taklit eder.

Desteklenmeyen veya doğrulanmamış bir canlı senaryoyu başarılı diye kaydetmeyin. Hata için uygulama/Windows/Codex/eklenti sürümlerini, kısa tekrar adımlarını ve kişisel verilerden arındırılmış sonucu belirtin.
