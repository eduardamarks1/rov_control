# Evolução da arquitetura do ROV Control

Este documento descreve as melhorias implementadas nos itens 1, 2, 3, 4, 5 e
9 do plano arquitetural. O sistema continua sendo didático e sem dependências
externas, mas agora cobre liderança, autorização efetiva, múltiplos ROVs,
configuração, vídeo de baixa latência e testes de falhas.

## 1. Split-brain, liderança e fencing

Cada relay mantém:

- `active`: somente o relay ativo atende clientes;
- `term`: geração monotônica da liderança;
- `leader_addr`: endereço conhecido do líder.

Um relay follower responde `not_leader` com o termo e o endereço do líder.
Pilotos e ROVs seguem esse redirecionamento e se registram novamente.

O backup promove a si próprio somente depois do timeout do primário e incrementa
o termo. Um primário recuperado inicia com termo antigo, recebe o heartbeat do
líder atual e se torna follower. Se dois nós aparecerem ativos no mesmo termo,
há desempate determinístico pelo endereço.

Toda concessão de controle produz uma `lease_id`. Todo comando enviado ao ROV
contém:

```json
{
  "type": "command",
  "term": 2,
  "lease_id": "token-aleatorio",
  "command_seq": 17
}
```

O ROV guarda o maior termo aceito e a sequência da lease atual. Comandos de
termos antigos, leases vazias e sequências repetidas são rejeitados. Isso impede
que um relay antigo continue comandando o veículo depois de perder a liderança.

Limite deliberado: trata-se de prevenção de split-brain para dois nós, não de
Raft nem consenso por quorum. O projeto não afirma implementar consenso.

## 2. Tokens efetivos e autenticação de ROV

O token emitido depois da autenticação do piloto agora é armazenado pelo cliente
e enviado em `command` e `release_control`. O relay compara o token em tempo
constante antes de autorizar a operação.

ROVs também executam desafio–resposta:

1. ROV envia `register`;
2. relay envia `auth_challenge` com nonce único;
3. ROV responde com `HMAC-SHA256(segredo_do_dispositivo, nonce)`;
4. relay só aceita telemetria e vídeo depois da validação.

Segredos podem ser fornecidos por variáveis de ambiente:

```powershell
$env:ROV_PILOT_A_PASSWORD = "senha-forte"
$env:ROV_PILOT_B_PASSWORD = "outra-senha"
$env:ROV_1_SECRET = "segredo-provisionado-no-rov1"
$env:ROV_2_SECRET = "segredo-provisionado-no-rov2"
python demo_dashboard.py --config demo_config.json
```

Os valores default existem exclusivamente para a apresentação local. Em
produção, devem ser removidos e substituídos por provisionamento de chaves,
cofre de segredos e QUIC com TLS 1.3. O QUIC-lite ainda não criptografa tráfego.

## 3. Múltiplos ROVs

O launcher lê listas de ROVs e pilotos. Cada piloto possui um `target` e cada ROV
tem sua própria lease. Pilotos diferentes podem controlar ROVs diferentes ao
mesmo tempo.

Exemplo em `demo_config.json`:

```json
{
  "rovs": [
    {"id": "rov1", "video": true},
    {"id": "rov2", "video": true}
  ],
  "pilots": [
    {"id": "pilotoA", "target": "rov1"},
    {"id": "pilotoB", "target": "rov2"}
  ]
}
```

O dashboard mantém uma coleção de ROVs, desenha cada um na topologia e encerra
todos corretamente. A cena aquática acompanha o primeiro ROV da configuração;
os demais aparecem na topologia e operam normalmente pela rede.

Execução:

```powershell
python run_demo.py --config demo_config.json
python demo_dashboard.py --config demo_config.json
```

## 4. Configuração e cenários

`demo_config.py` centraliza carregamento e validação. São validados:

- quantidade mínima de relays;
- portas duplicadas;
- IDs duplicados de ROV;
- piloto apontando para ROV inexistente;
- perda de pacotes fora de `0..1`.

`run_demo.py` agora resolve scripts relativamente ao próprio arquivo, portanto
não depende do diretório de execução. Ele também encerra processos com
`terminate`, espera até três segundos e usa `kill` como último recurso.

Cenários:

```powershell
# Interação manual
python run_demo.py --config demo_config.json --scenario manual

# Derruba automaticamente o primário depois de oito segundos
python run_demo.py --config demo_config.json --scenario failover

# Mantém a configuração antiga e adiciona concorrência pelo mesmo ROV
python run_demo.py --two-pilots
```

## 5. Streaming de vídeo

`video_stream.py` gera uma câmera submarina sintética em PPM para não introduzir
OpenCV, Pillow ou codecs externos. O objetivo é demonstrar características do
transporte, não qualidade de compressão.

Pipeline:

```text
ROV gera frame PPM
  -> base64
  -> chunks de até 850 caracteres
  -> datagramas não confiáveis
  -> relay encaminha ao piloto controlador
  -> piloto remonta ou expira o frame
  -> dashboard mostra frame, latência e descartes
```

Cada chunk possui `rov`, `frame_id`, `chunk_id`, `chunk_count` e `sent_at`.
Frames incompletos expiram em um segundo. Não há retransmissão: um frame novo é
mais útil do que recuperar um frame velho. Comandos continuam no canal
confiável e não aguardam frames perdidos.

Essa separação motiva o QUIC-lite:

| Tráfego | Garantia | Motivo |
|---|---|---|
| autenticação e comando | confiável e ordenado | não pode sumir nem inverter |
| telemetria | datagrama | o valor mais recente vence |
| vídeo | datagrama fragmentado | baixa latência é mais importante que recuperar frame antigo |

O frame sintético padrão tem 96×72 pixels e é enviado a aproximadamente 2 FPS.
Para vídeo real, o passo seguinte é usar H.264/VP9/AV1 e QUIC DATAGRAM real.

## 9. Testes

Teste legado:

```powershell
python test_system.py
```

Valida autenticação de piloto, exclusão mútua, comando, replicação e failover.

Teste ampliado:

```powershell
python test_extended.py
```

Valida:

- remontagem de vídeo fora de ordem;
- autenticação simultânea de dois ROVs;
- dois pilotos controlando ROVs independentes;
- rejeição de segredo de dispositivo inválido;
- recepção de vídeo;
- rejeição de comando sem token;
- redirecionamento de cliente conectado ao follower;
- promoção do backup com incremento de termo;
- migração de clientes;
- rejeição de comando de termo antigo;
- retorno do primário sem split-brain.

Os testes usam portas 5100/5101 e 5200/5201 para não colidir com a demo.

## Arquivos alterados ou adicionados

- `protocol.py`: credenciais por ambiente e autenticação de ROV;
- `relay_server.py`: liderança, termos, tokens, leases, fencing e vídeo;
- `rov_simulator.py`: autenticação, fencing e câmera sintética;
- `pilot_client.py`: token efetivo, sequência de comandos e receptor de vídeo;
- `video_stream.py`: geração, fragmentação e remontagem;
- `demo_config.py` e `demo_config.json`: configuração declarativa;
- `run_demo.py`: launcher multi-ROV e cenários;
- `demo_dashboard.py`: topologia multi-ROV e painel de vídeo;
- `test_extended.py`: segurança, falhas, vídeo e multi-ROV;
- `.gitignore`: artefatos Python e logs.

## Limitações restantes

- O transporte não oferece criptografia, congestion control ou QUIC/TLS real.
- Dois relays não formam quorum; uma implementação de consenso exigiria três.
- O estado replicado ainda é baseado em eventos e não possui snapshot durável.
- Os segredos default são somente para demonstração.
- A GUI mostra uma cena aquática principal, embora a topologia suporte vários
  ROVs.
