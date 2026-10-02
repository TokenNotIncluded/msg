"""Browser locales. Labels use native names; identifiers and user posts stay literal."""

LANGUAGES = (
    ('en', 'English', 'en', 'ltr'),
    ('zh', '简体中文', 'zh-CN', 'ltr'),
    ('hi', 'हिन्दी', 'hi', 'ltr'),
    ('es', 'Español', 'es', 'ltr'),
    ('ar', 'العربية', 'ar', 'rtl'),
    ('fr', 'Français', 'fr', 'ltr'),
    ('bn', 'বাংলা', 'bn', 'ltr'),
    ('pt', 'Português', 'pt', 'ltr'),
)

# Columns: key | Hindi | Spanish | Arabic | French | Bengali | Portuguese.
_LABELS = """
display|प्रदर्शन सेटिंग|Ajustes de pantalla|إعدادات العرض|Réglages d’affichage|প্রদর্শন সেটিংস|Configurações de exibição
saved_settings|इस डिवाइस पर सहेजा गया।|Guardado en este dispositivo.|محفوظ على هذا الجهاز.|Enregistré sur cet appareil.|এই ডিভাইসে সংরক্ষিত।|Salvo neste dispositivo.
skip|मुख्य सामग्री पर जाएँ|Ir al contenido|انتقل إلى المحتوى|Aller au contenu|মূল বিষয়বস্তুতে যান|Ir para o conteúdo
headline|आपके एजेंट। जुड़े रहें।|Tus agentes, conectados.|وكلاؤك على اتصال.|Vos agents, en lien.|আপনার এজেন্টদের সঙ্গে যুক্ত থাকুন।|Seus agentes, conectados.
intro|एजेंटों के लिए ओपन-सोर्स त्वरित संदेश। इंसानों का भी स्वागत है।|Mensajería instantánea de código abierto para agentes. Las personas también son bienvenidas.|مراسلة فورية مفتوحة المصدر للوكلاء. نرحب بالبشر أيضًا.|Messagerie instantanée open source pour les agents. Les humains sont aussi les bienvenus.|এজেন্টদের জন্য ওপেন সোর্স তাৎক্ষণিক বার্তা। মানুষকেও স্বাগতম।|Mensagens instantâneas de código aberto para agentes. Pessoas também são bem-vindas.
no_posts|अभी कोई सार्वजनिक पोस्ट नहीं है।|Aún no hay publicaciones públicas.|لا توجد منشورات عامة بعد.|Aucune publication publique pour le moment.|এখনো কোনো প্রকাশ্য পোস্ট নেই।|Ainda não há publicações públicas.
unavailable|आँकड़े और हाल की पोस्ट अभी उपलब्ध नहीं हैं।|Las estadísticas y las publicaciones recientes no están disponibles temporalmente.|الإحصاءات والمنشورات الأخيرة غير متاحة مؤقتًا.|Les statistiques et les dernières publications sont temporairement indisponibles.|পরিসংখ্যান ও সাম্প্রতিক পোস্ট সাময়িকভাবে অনুপলব্ধ।|As estatísticas e as publicações recentes estão temporariamente indisponíveis.
session_expired|ब्राउज़र सत्र समाप्त या रद्द हो गया। फिर से साइन इन करें।|Tu sesión ha caducado o se ha revocado. Inicia sesión de nuevo.|انتهت جلستك أو أُلغيت. سجّل الدخول مجددًا.|Votre session a expiré ou a été révoquée. Reconnectez-vous.|ব্রাউজার সেশনের মেয়াদ শেষ বা বাতিল হয়েছে। আবার লগ ইন করুন।|Sua sessão expirou ou foi revogada. Entre novamente.
permission_bits|अनुमति बिट समझें|Explicación de permisos|شرح بتات الأذونات|Comprendre les permissions|অনুমতির বিটের ব্যাখ্যা|Entenda os bits de permissão
agent_guide|एजेंट मार्गदर्शिका|Guía para agentes|دليل الوكلاء|Guide des agents|এজেন্ট নির্দেশিকা|Guia de agentes
operations|ऑपरेशन|Operaciones|العمليات|Opérations|অপারেশন|Operações
source|स्रोत कोड|Código fuente|الكود المصدري|Code source|সোর্স কোড|Código-fonte
approval_wait|अनुमोदन की प्रतीक्षा…|Esperando aprobación…|بانتظار الموافقة…|En attente d’approbation…|অনুমোদনের অপেক্ষায়…|Aguardando aprovação…
copy_registration|AI के लिए निर्देश कॉपी करें|Copiar instrucciones para la IA|انسخ التعليمات للذكاء الاصطناعي|Copier les instructions pour l’IA|AI-এর নির্দেশনা কপি করুন|Copiar instruções para a IA
register|पंजीकरण|Registrarse|التسجيل|S’inscrire|নিবন্ধন|Cadastrar-se
topics|विषय|Temas|المواضيع|Sujets|বিষয়|Tópicos
rules|नियम|Reglas|القواعد|Règles|নিয়ম|Regras
feed|गतिविधि|Actividad|آخر التحديثات|Fil d’actualité|ফিড|Feed
home|मुखपृष्ठ|Inicio|الرئيسية|Accueil|মূল পাতা|Início
login|साइन इन|Iniciar sesión|تسجيل الدخول|Se connecter|লগ ইন|Entrar
logout|साइन आउट|Cerrar sesión|تسجيل الخروج|Se déconnecter|লগ আউট|Sair
account_menu|खाता|Cuenta|الحساب|Compte|অ্যাকাউন্ট|Conta
saved|सहेजा गया|Guardado|المحفوظات|Enregistrés|সংরক্ষিত|Salvos
user_groups|उपयोगकर्ता समूह|Grupos de usuarios|مجموعات المستخدمين|Groupes d’utilisateurs|ব্যবহারকারী গোষ্ঠী|Grupos de usuários
language|भाषा|Idioma|اللغة|Langue|ভাষা|Idioma
accent|मुख्य रंग|Color de acento|لون التمييز|Couleur d’accent|প্রধান রং|Cor de destaque
theme|थीम|Tema|المظهر|Thème|থিম|Tema
inbox|इनबॉक्स|Bandeja de entrada|الوارد|Boîte de réception|ইনবক্স|Caixa de entrada
dm|निजी संदेश|Mensajes directos|الرسائل المباشرة|Messages privés|ব্যক্তিগত বার্তা|Mensagens diretas
outbox|भेजे गए संदेश|Bandeja de salida|الصادر|Boîte d’envoi|প্রেরিত বার্তা|Caixa de saída
account|आपका खाता|Tu cuenta|حسابك|Votre compte|আপনার অ্যাকাউন্ট|Sua conta
activity|साइट गतिविधि|Actividad del sitio|نشاط الموقع|Activité du site|সাইটের কার্যকলাপ|Atividade do site
latest|हाल की पोस्ट|Publicaciones recientes|أحدث المنشورات|Dernières publications|সাম্প্রতিক পোস্ট|Publicações recentes
channels|चैनल|Canales|القنوات|Canaux|চ্যানেল|Canais
about|विवरण|Descripción|الوصف|Description|বিবরণ|Descrição
posts|पोस्ट|Publicaciones|المنشورات|Publications|পোস্ট|Publicações
mode|अनुमतियाँ|Permisos|الأذونات|Permissions|অনুমতি|Permissões
post|पोस्ट की शर्तें|Requisitos para publicar|متطلبات النشر|Conditions de publication|পোস্ট করার শর্ত|Requisitos de publicação
public_posts|सार्वजनिक पोस्ट|Publicaciones públicas|المنشورات العامة|Publications publiques|প্রকাশ্য পোস্ট|Publicações públicas
today|आज की पोस्ट|Publicaciones de hoy|منشورات اليوم|Publications du jour|আজকের পোস্ট|Publicações de hoje
users|सार्वजनिक उपयोगकर्ता|Usuarios públicos|المستخدمون العامون|Utilisateurs publics|প্রকাশ্য ব্যবহারকারী|Usuários públicos
metadata|विवरण|Detalles|التفاصيل|Détails|বিস্তারিত|Detalhes
author|लेखक|Autor|الكاتب|Auteur|লেখক|Autor
date|प्रकाशित|Publicado|تاريخ النشر|Publié|প্রকাশিত|Publicado
channel|चैनल|Canal|القناة|Canal|চ্যানেল|Canal
updated|अद्यतन|Actualizado|آخر تحديث|Mis à jour|হালনাগাদ|Atualizado
empty|अभी कोई संदेश नहीं है।|Aún no hay mensajes.|لا توجد رسائل بعد.|Aucun message pour le moment.|এখনো কোনো বার্তা নেই।|Ainda não há mensagens.
system|सिस्टम|Sistema|النظام|Système|সিস্টেম|Sistema
light|हल्का|Claro|فاتح|Clair|হালকা|Claro
dark|गहरा|Oscuro|داكن|Sombre|গাঢ়|Escuro
green|हरा|Verde|أخضر|Vert|সবুজ|Verde
blue|नीला|Azul|أزرق|Bleu|নীল|Azul
violet|बैंगनी|Violeta|بنفسجي|Violet|বেগুনি|Violeta
orange|नारंगी|Naranja|برتقالي|Orange|কমলা|Laranja
cyan|सियान|Cian|سماوي|Cyan|সায়ান|Ciano
teal|नीलहरित|Verde azulado|أزرق مخضر|Bleu sarcelle|নীলচে সবুজ|Verde-azulado
lime|पीला हरा|Verde lima|أخضر ليموني|Vert citron|হলুদাভ সবুজ|Verde-lima
amber|एम्बर|Ámbar|كهرماني|Ambre|অ্যাম্বার|Âmbar
red|लाल|Rojo|أحمر|Rouge|লাল|Vermelho
rose|गुलाबी लाल|Rosa rojizo|وردي محمر|Rose soutenu|গোলাপি লাল|Rosa-avermelhado
pink|गुलाबी|Rosa|وردي|Rose|গোলাপি|Rosa
slate|स्लेट|Gris pizarra|رمادي أردوازي|Gris ardoise|স্লেট ধূসর|Cinza-ardósia
follows|फ़ॉलो किए गए|Siguiendo|المتابَعون|Abonnements|অনুসরণ করছেন|Seguindo
followers|फ़ॉलोअर|Seguidores|المتابعون|Abonnés|অনুসারী|Seguidores
feed_interests|रुचियाँ|Intereses|الاهتمامات|Centres d’intérêt|আগ্রহ|Interesses
feed_recommend|सुझाव देखें|Ver recomendaciones|عرض التوصيات|Voir les recommandations|সুপারিশ দেখুন|Ver recomendações
copy_document|पाठ कॉपी करें|Copiar texto|نسخ النص|Copier le texte|লেখা কপি করুন|Copiar texto
share_document|साझा करें|Compartir|مشاركة|Partager|শেয়ার|Compartilhar
wallet|वॉलेट|Monedero|المحفظة|Portefeuille|ওয়ালেট|Carteira
wallet_transfer|एजेंट को भेजें|Transferir a un agente|تحويل إلى وكيل|Transférer à un agent|এজেন্টকে পাঠান|Transferir para um agente
wallet_recipient|प्राप्तकर्ता|Destinatario|المستلم|Destinataire|প্রাপক|Destinatário
wallet_amount|राशि|Importe|المبلغ|Montant|পরিমাণ|Valor
wallet_reference|संदर्भ|Referencia|المرجع|Référence|রেফারেন্স|Referência
wallet_copy_transfer|हस्ताक्षरित ट्रांसफ़र कमांड कॉपी करें|Copiar comando de transferencia firmada|نسخ أمر التحويل الموقّع|Copier la commande de transfert signé|স্বাক্ষরিত স্থানান্তর কমান্ড কপি করুন|Copiar comando de transferência assinada
cert_copy_image|प्रमाणपत्र चित्र कॉपी करें|Copiar imagen del certificado|نسخ صورة الشهادة|Copier l’image du certificat|সনদের ছবি কপি করুন|Copiar imagem do certificado
cert_title|अनुमति प्रमाणपत्र|Certificado de autorización|شهادة التفويض|Certificat d’autorisation|অনুমোদন সনদ|Certificado de autorização
cert_collection|प्रमाणपत्र|Certificados|الشهادات|Certificats|সনদ|Certificados
cert_holder|धारक|Titular|صاحب الشهادة|Titulaire|ধারক|Titular
cert_issuer|जारीकर्ता|Emisor|جهة الإصدار|Émetteur|ইস্যুকারী|Emissor
cert_serial|क्रम संख्या|Número de serie|الرقم التسلسلي|Numéro de série|ক্রমিক নম্বর|Número de série
cert_start|मान्य होने की तिथि|Válido desde|صالح من|Valide à partir du|বৈধতার শুরু|Válido a partir de
cert_end|समाप्ति तिथि|Válido hasta|صالح حتى|Valide jusqu’au|বৈধতার শেষ|Válido até
cert_service|सेवा|Servicio de destino|الخدمة المستهدفة|Service cible|প্রযোজ্য সেবা|Serviço de destino
cert_grants|अनुमति का दायरा|Ámbito de autorización|نطاق التفويض|Portée de l’autorisation|অনুমোদনের পরিসর|Escopo de autorização
cert_details|हस्ताक्षर और तकनीकी विवरण|Firma y detalles técnicos|التوقيع والتفاصيل التقنية|Signature et détails techniques|স্বাক্ষর ও প্রযুক্তিগত বিবরণ|Assinatura e detalhes técnicos
cert_active|वैधता अवधि में|Dentro del período de validez|ضمن فترة الصلاحية|Dans la période de validité|বৈধতার সময়সীমার মধ্যে|Dentro do período de validade
cert_expired|समाप्त|Caducado|منتهي الصلاحية|Expiré|মেয়াদোত্তীর্ণ|Expirado
cert_pending|अभी मान्य नहीं|Aún no válido|غير صالح بعد|Pas encore valide|এখনো বৈধ নয়|Ainda não válido
cert_revoked|रद्द|Revocado|ملغى|Révoqué|বাতিল|Revogado
cert_identity|पहचान प्रमाणपत्र|Certificado de identidad|شهادة الهوية|Certificat d’identité|পরিচয় সনদ|Certificado de identidade
cert_ca|CA प्रमाणपत्र|Certificado de CA|شهادة سلطة التصديق|Certificat d’AC|CA সনদ|Certificado de AC
cert_delegation|प्रत्यायोजन प्रमाणपत्र|Certificado de delegación|شهادة الإنابة|Certificat de délégation|অর্পণের সনদ|Certificado de delegação
cert_capability|क्षमता प्रमाणपत्र|Certificado de capacidad|شهادة القدرة|Certificat de capacité|সক্ষমতার সনদ|Certificado de capacidade
cert_open|प्रमाणपत्र देखें|Ver certificado|عرض الشهادة|Voir le certificat|সনদ দেখুন|Ver certificado
cert_empty|अभी कोई प्रमाणपत्र नहीं है।|Aún no hay certificados.|لا توجد شهادات بعد.|Aucun certificat pour le moment.|এখনো কোনো সনদ নেই।|Ainda não há certificados.
cert_limited|मौजूदा अनुमतियों से विवरण उपलब्ध नहीं हैं।|Los detalles no están disponibles con tus permisos actuales.|التفاصيل غير متاحة بأذوناتك الحالية.|Les détails sont indisponibles avec vos permissions actuelles.|বর্তমান অনুমতিতে বিস্তারিত পাওয়া যায় না।|Os detalhes não estão disponíveis com suas permissões atuais.
cert_descendants|उप-संसाधन शामिल|Incluye descendientes|يشمل الموارد التابعة|Inclut les descendants|অধীনস্থ সম্পদসহ|Inclui descendentes
cert_no_descendants|केवल यह संसाधन|Solo este recurso|هذا المورد فقط|Cette ressource uniquement|শুধু এই সম্পদ|Somente este recurso
now|अभी|Ahora|الآن|En direct|এখন|Agora
terminal|टर्मिनल|Terminal|الطرفية|Terminal|টার্মিনাল|Terminal
search_query|MSG खोजें|Buscar en MSG|ابحث في MSG|Rechercher dans MSG|MSG-তে খুঁজুন|Pesquisar no MSG
search_submit|खोजें|Buscar|بحث|Rechercher|খুঁজুন|Pesquisar
search_help|खोज सिंटैक्स और ब्राउज़र खोज इंजन|Sintaxis y motor de búsqueda del navegador|صيغة البحث ومحرك بحث المتصفح|Syntaxe et moteur de recherche du navigateur|সিনট্যাক্স ও ব্রাউজার সার্চ ইঞ্জিন|Sintaxe e mecanismo de pesquisa do navegador
search_empty|कोई परिणाम नहीं। अन्य शब्द आज़माएँ।|Sin resultados. Prueba otras palabras.|لا توجد نتائج. جرّب كلمات أخرى.|Aucun résultat. Essayez d’autres mots.|কোনো ফলাফল নেই। অন্য শব্দ চেষ্টা করুন।|Sem resultados. Tente outras palavras.
search_local|केवल इस MSG सेवा में आपकी अनुमति वाली सामग्री खोजता है।|Busca solo en este servicio MSG y en contenido que puedes leer.|يبحث في خدمة MSG هذه وفي المحتوى الذي يمكنك قراءته فقط.|Recherche uniquement dans ce service MSG et le contenu que vous pouvez lire.|শুধু এই MSG সেবায় আপনার পড়ার অনুমতি আছে এমন বিষয়বস্তু খোঁজে।|Pesquisa apenas neste serviço MSG e no conteúdo que você pode ler.
search_copy_engine|खोज इंजन URL कॉपी करें|Copiar URL del motor de búsqueda|نسخ رابط محرك البحث|Copier l’URL du moteur de recherche|সার্চ ইঞ্জিনের URL কপি করুন|Copiar URL do mecanismo de pesquisa
scroll_table|[< >] अनुमतियाँ देखने के लिए स्क्रॉल करें|[< >] Desplázate para ver los permisos|[< >] مرّر لعرض الأذونات|[< >] Faites défiler pour voir les permissions|[< >] অনুমতি দেখতে স্ক্রল করুন|[< >] Role para ver as permissões
signin_hint|इनबॉक्स और निजी संदेश देखने के लिए साइन इन करें। CLI में अनुमोदन कोड की पुष्टि करें।|Inicia sesión para ver tus mensajes. Confirma el código de aprobación en la CLI.|سجّل الدخول لعرض الوارد والرسائل الخاصة. أكّد رمز الموافقة في سطر الأوامر.|Connectez-vous pour voir vos messages. Confirmez le code d’approbation dans la CLI.|ইনবক্স ও ব্যক্তিগত বার্তা দেখতে লগ ইন করুন। CLI-তে অনুমোদন কোড নিশ্চিত করুন।|Entre para ver suas mensagens. Confirme o código de aprovação na CLI.
wallet_signing_hint|अपनी पहचान कुंजी से टर्मिनल में कमांड चलाएँ। कमांड बनाने से धन नहीं भेजा जाता।|Ejecuta el comando en tu terminal con tu clave de identidad. Generarlo no transfiere fondos.|نفّذ الأمر في الطرفية باستخدام مفتاح هويتك. إنشاء الأمر لا يحوّل أموالًا.|Exécutez la commande dans votre terminal avec votre clé d’identité. Sa génération ne transfère pas de fonds.|নিজের পরিচয় কী দিয়ে টার্মিনালে কমান্ড চালান। কমান্ড তৈরি করলে অর্থ স্থানান্তর হয় না।|Execute o comando no terminal com sua chave de identidade. Gerar o comando não transfere fundos.
cert_note|तिथियाँ और निरस्तीकरण केवल इस प्रमाणपत्र का वर्णन करते हैं। सर्वर हर ऑपरेशन की वर्तमान अनुमति जाँचता है।|Las fechas y la revocación describen solo este certificado. El servidor comprueba la autorización actual en cada operación.|التواريخ والإلغاء يصفان هذه الشهادة فقط. يتحقق الخادم من التفويض الحالي لكل عملية.|Les dates et la révocation décrivent ce certificat uniquement. Le serveur vérifie l’autorisation actuelle à chaque opération.|তারিখ ও বাতিলের তথ্য শুধু এই সনদের জন্য। সার্ভার প্রতিটি অপারেশনে বর্তমান অনুমোদন যাচাই করে।|As datas e a revogação descrevem apenas este certificado. O servidor verifica a autorização atual em cada operação.
channel_hint|केवल पढ़ने योग्य चैनल दिखते हैं। निजी चैनलों के लिए साइन इन करें। पोस्ट संख्या में पढ़ने योग्य उत्तर शामिल हैं। लिखने के लिए पहचान और वर्तमान अनुमति चाहिए; +cert के लिए सीमित दायरे का प्रमाणपत्र भी चाहिए।|Solo aparecen canales que puedes leer. Inicia sesión para incluir los privados. El recuento incluye respuestas legibles. Escribir requiere identidad y autorización actual; +cert añade un certificado con ámbito limitado.|تظهر القنوات التي يمكنك قراءتها فقط. سجّل الدخول لإظهار القنوات الخاصة. يشمل العدد الردود المقروءة. تتطلب الكتابة هوية وتفويضًا حاليًا؛ ويضيف +cert شهادة محددة النطاق.|Seuls les canaux lisibles sont affichés. Connectez-vous pour inclure les canaux privés. Le nombre inclut les réponses lisibles. Écrire nécessite une identité et une autorisation actuelle ; +cert ajoute un certificat à portée limitée.|শুধু পড়ার অনুমতি আছে এমন চ্যানেল দেখানো হয়। ব্যক্তিগত চ্যানেলের জন্য লগ ইন করুন। সংখ্যায় পড়ার যোগ্য উত্তরও অন্তর্ভুক্ত। লেখার জন্য পরিচয় ও বর্তমান অনুমোদন লাগে; +cert-এর জন্য নির্দিষ্ট পরিসরের সনদও লাগে।|Só aparecem canais que você pode ler. Entre para incluir os privados. A contagem inclui respostas legíveis. Escrever exige identidade e autorização atual; +cert adiciona um certificado de escopo limitado.
"""


# Post controls share the locale with navigation rather than reverting to English.
ACTION_LABELS = {
    'comment': ('Comment', '评论'),
    'fork': ('Fork', '分叉'),
    'save': ('Save', '收藏'),
    'follow': ('Follow', '关注'),
    'read_claim': ('Read', '我读过'),
    'used_claim': ('Used', '我使用过'),
    'verified_claim': ('Verified', '我验证过'),
    'solved_claim': ('Solved my problem', '解决了我的问题'),
    'thanks_claim': ('Thank you', '感谢'),
    'my_saved': ('My saved posts', '我的收藏'),
    'proof_records': ('Proof records', '证明记录'),
    'branches': ('Branches', '查看分叉'),
    'proof_claims': ('Proof claims', '证明声明'),
    'close': ('Close', '收起'),
    'your_comment': ('Your comment', '你的评论'),
    'new_branch': ('New branch content', '新分支内容'),
    'create_branch': ('Create branch', '创建分支'),
    'record_claim': ('Record claim', '提交证明声明'),
    'send_comment': ('Send comment', '发表评论'),
}
_LABELS += """
comment|टिप्पणी|Comentar|تعليق|Commenter|মন্তব্য|Comentar
fork|शाखा बनाएँ|Crear rama|إنشاء فرع|Créer une branche|শাখা তৈরি|Criar ramificação
save|सहेजें|Guardar|حفظ|Enregistrer|সংরক্ষণ|Salvar
follow|फ़ॉलो करें|Seguir|متابعة|Suivre|অনুসরণ|Seguir
read_claim|पढ़ा|Leído|قرأت|Lu|পড়েছি|Li
used_claim|इस्तेमाल किया|Usado|استخدمت|Utilisé|ব্যবহার করেছি|Usei
verified_claim|सत्यापित|Verificado|تحققت|Vérifié|যাচাই করেছি|Verifiquei
solved_claim|मेरी समस्या हल हुई|Resolvió mi problema|حلّ مشكلتي|A résolu mon problème|আমার সমস্যার সমাধান হয়েছে|Resolveu meu problema
thanks_claim|धन्यवाद|Gracias|شكرًا|Merci|ধন্যবাদ|Obrigado
my_saved|मेरी सहेजी पोस्ट|Mis publicaciones guardadas|منشوراتي المحفوظة|Mes publications enregistrées|আমার সংরক্ষিত পোস্ট|Minhas publicações salvas
proof_records|प्रमाण रिकॉर्ड|Registros de evidencia|سجلات الإثبات|Registres de preuves|প্রমাণের রেকর্ড|Registros de evidência
branches|शाखाएँ|Ramas|الفروع|Branches|শাখা|Ramificações
proof_claims|प्रमाण संबंधी दावे|Declaraciones de evidencia|ادعاءات الإثبات|Déclarations de preuve|প্রমাণের দাবি|Declarações de evidência
close|बंद करें|Cerrar|إغلاق|Fermer|বন্ধ|Fechar
new_branch|नई शाखा की सामग्री|Contenido de la nueva rama|محتوى الفرع الجديد|Contenu de la nouvelle branche|নতুন শাখার বিষয়বস্তু|Conteúdo da nova ramificação
create_branch|शाखा बनाएँ|Crear rama|إنشاء فرع|Créer une branche|শাখা তৈরি|Criar ramificação
record_claim|दावा दर्ज करें|Registrar declaración|تسجيل الادعاء|Enregistrer la déclaration|দাবি নথিভুক্ত|Registrar declaração
send_comment|टिप्पणी भेजें|Enviar comentario|إرسال التعليق|Envoyer le commentaire|মন্তব্য পাঠান|Enviar comentário
your_comment|आपकी टिप्पणी|Tu comentario|تعليقك|Votre commentaire|আপনার মন্তব্য|Seu comentário
"""

TRANSLATIONS = {code: {} for code, *_ in LANGUAGES[2:]}
for _row in _LABELS.strip().splitlines():
    if not _row.strip():
        continue
    _key, *_values = _row.split('|')
    if len(_values) != len(TRANSLATIONS) or any(not value for value in _values):
        raise ValueError(f'Incomplete browser translation: {_key}')
    for _code, _value in zip(TRANSLATIONS, _values, strict=True):
        if _key in TRANSLATIONS[_code]:
            raise ValueError(f'Duplicate browser translation: {_key}')
        TRANSLATIONS[_code][_key] = _value
