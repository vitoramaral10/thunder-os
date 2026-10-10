#!/usr/bin/env python3
"""Ponte HCI entre /dev/stpbt (MediaTek) e /dev/vhci (BlueZ).

POR QUE ISTO EXISTE
-------------------
O driver de Bluetooth do MT6833 (bt_drv_6833.ko) e' o mesmo que o Android usa:
ele expoe um dispositivo de caractere, /dev/stpbt, e nada mais.  A pilha do
Android e' toda em espaco de usuario e fala direto com esse no.  O BlueZ nao:
ele quer um "hci_dev" registrado no kernel.

O upstream da MediaTek tem uma variante que registra hci_dev (o Kconfig
descreve CONFIG_MTK_COMBO_BT_HCI como "MTK BT driver for BlueZ"), mas o codigo
dela nao esta publicado em nenhuma das arvores a que temos acesso -- so' a
opcao no Kconfig sobreviveu.  Entao a ligacao e' feita aqui, em espaco de
usuario, usando o hci_vhci do proprio kernel: criamos um controlador virtual
e repassamos os pacotes nos dois sentidos.

O CUIDADO QUE NAO E' OBVIO
--------------------------
Os dois lados NAO tem o mesmo contrato:

  /dev/vhci  entrega e aceita UM pacote HCI completo por read()/write().
  /dev/stpbt entrega um FLUXO de bytes.  O read() devolve o que estiver na
             fila de RX da STP, que pode conter dois eventos colados ou um
             evento pela metade.

Copiar byte a byte de um lado para o outro parece funcionar -- ate' o primeiro
momento de trafego intenso, quando dois eventos chegam juntos e o kernel
rejeita o write com -EINVAL, ou pior, interpreta o segundo evento como corpo
do primeiro.  Por isso o sentido controlador->host remonta o enquadramento H4
antes de escrever.

O sentido host->controlador nao precisa disso: o read() do vhci ja' e' por
pacote e a STP aceita o frame H4 inteiro numa escrita so'.

POR QUE select() E NAO UM read() BLOQUEANTE POR SENTIDO
-------------------------------------------------------
Esta foi a licao mais cara do bring-up, e vale escrever inteira.

A primeira versao usava duas threads, cada uma parada num os.read() bloqueante.
Funcionou -- ate' a primeira vez que a ponte precisou reiniciar.  Ao receber o
sinal, o processo virou zumbi e NAO morreu.  Estado medido:

    tid=3342  state=Z (zombie)
    tid=3347  state=D (disk sleep)   wchan=BT_read

A causa esta' no driver: BT_read() espera com wait_event(), que e'
NAO INTERROMPIVEL.  Com o controlador em silencio nada acorda a fila, entao a
thread fica presa dentro do kernel para sempre -- nem SIGKILL a tira de la'.
E enquanto ela nao sai:

  * os descritores nunca fecham;
  * BT_release() nunca roda, entao o btonflag do driver fica em 1 e toda
    abertura seguinte de /dev/stpbt devolve EIO ("BT already on!");
  * /dev/vhci tambem fica aberto, entao o hci0 continua registrado, orfao,
    sem ninguem do outro lado.

O aparelho so' volta ao normal reiniciando.

A correcao e' nao entrar nesse estado: o driver implementa poll(), entao a
ponte espera em select() e so' chama read() quando ja' ha' dado.  Um autopipe
entra no mesmo select para o desligamento, o que dispensa as threads por
completo -- um laco so', que sempre pode sair e fechar os descritores em ordem.
"""

import os
import select
import struct
import signal
import sys

STPBT = "/dev/stpbt"
VHCI = "/dev/vhci"

# Tipos de pacote H4 e o tamanho do cabecalho que vem depois do byte de tipo.
# O ultimo campo diz em quantos bytes o comprimento do corpo esta escrito.
#                       tipo: (bytes de cabecalho, offset do campo de tamanho,
#                              largura do campo)
H4 = {
    0x01: (3, 2, 1),   # Command:    opcode(2) + plen(1)
    0x02: (4, 2, 2),   # ACL data:   handle(2) + dlen(2)
    0x03: (3, 2, 1),   # SCO data:   handle(2) + dlen(1)
    0x04: (2, 1, 1),   # Event:      evento(1) + plen(1)
    0x05: (4, 2, 2),   # ISO data:   handle(2) + dlen(2)
}

VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv


def log(msg):
    print("[ponte] %s" % msg, flush=True)


def dbg(msg):
    if VERBOSE:
        print("[ponte] %s" % msg, flush=True)


def tamanho_do_pacote(buf):
    """Devolve o tamanho total do primeiro pacote H4 em buf.

    Retorna None quando ainda nao ha' bytes suficientes nem para ler o
    cabecalho ou o corpo.  Levanta ValueError para um byte de tipo invalido,
    porque nesse ponto o fluxo esta' dessincronizado e continuar so' propaga
    o erro.
    """
    tipo = buf[0]
    if tipo not in H4:
        raise ValueError("byte de tipo H4 invalido: 0x%02x" % tipo)

    hdr, off, largura = H4[tipo]
    if len(buf) < 1 + hdr:
        return None

    corpo = int.from_bytes(buf[1 + off:1 + off + largura], "little")
    if tipo == 0x05:
        corpo &= 0x3FFF  # ISO: os 2 bits altos sao flags, nao tamanho

    total = 1 + hdr + corpo
    return total if len(buf) >= total else None


# ---------------------------------------------------------------------------
# Contorno de controlador: o bit LMP_SYNC_TRAIN e' mentira neste chip
# ---------------------------------------------------------------------------
# Sintoma: o adaptador nunca sobe.  hci0 aparece em /sys/class/bluetooth, o
# rfkill e' criado, a sequencia de inicializacao roda inteira -- e no fim o
# bluetoothd ve "Number of controllers: 0", porque o mgmt_index_added do kernel
# so' acontece quando a abertura termina bem.
#
# Medido: HCIDEVUP devolve errno 56 (EBADRQC).  Nao e' um erro generico --
# bt_to_errno() do kernel mapeia exatamente o status HCI 0x01 ("Unknown HCI
# Command") para EBADRQC.  E o ultimo par de pacotes antes da falha e':
#
#     -> 01 77 0c 00                 Read_Sync_Train_Params (0x0C77)
#     <- 04 0f 04 01 01 77 0c        Command Status, status 0x01
#
# A causa esta' uma resposta antes:
#
#     -> 01 04 10 01 02              Read_Local_Extended_Features, pagina 2
#     <- ... 02 02 05 03 00 ...      features[0] = 0x05
#
# 0x05 = LMP_CSB_MASTER (0x01) | LMP_SYNC_TRAIN (0x04).  O chip DECLARA
# suportar Synchronization Train.  O kernel confia nesse bit --
# hci_init4_req() faz "if (lmp_sync_train_capable(hdev))" e so' entao manda o
# 0x0C77 -- e o chip entao rejeita o proprio comando que disse suportar.  Um
# comando com erro no meio da sequencia derruba a abertura toda.
#
# E' um bug de firmware que nunca aparece no Android: a pilha de la nao usa
# esse caminho, entao ninguem nunca mandou esse comando neste chip.
#
# POR QUE LIMPAR O BIT, e nao forjar a resposta do 0x0C77:
# responder "sucesso" a um comando que o controlador nao implementa faria o
# kernel acreditar em parametros de sync train que nao existem.  Limpar o bit
# conta a verdade -- o controlador nao sabe fazer isso -- e deixa toda a
# decisao seguinte com a logica do proprio kernel, que entao nem chega a
# mandar o comando.
#
# Num driver de kernel isto seria um HCI_QUIRK_*.  Como aqui a ponte faz o
# papel do driver, o contorno mora aqui.

OP_READ_LOCAL_EXT_FEATURES = 0x1004
LMP_SYNC_TRAIN = 0x04


def corrige_features(pkt):
    """Zera LMP_SYNC_TRAIN na pagina 2 de Read_Local_Extended_Features.

    Devolve o pacote (alterado ou nao) e True quando mexeu.
    """
    #  0     1     2      3      4  5      6       7      8         9..16
    # tipo  evt  plen  ncmd  opcode  status  page  max_page  features[8]
    if len(pkt) < 17 or pkt[0] != 0x04 or pkt[1] != 0x0E:
        return pkt, False
    if int.from_bytes(pkt[4:6], "little") != OP_READ_LOCAL_EXT_FEATURES:
        return pkt, False
    if pkt[6] != 0x00 or pkt[7] != 0x02:      # status != sucesso, ou nao e' a pagina 2
        return pkt, False
    if not pkt[9] & LMP_SYNC_TRAIN:
        return pkt, False
    novo = bytearray(pkt)
    novo[9] &= ~LMP_SYNC_TRAIN
    return bytes(novo), True


# ---------------------------------------------------------------------------
# Endereco Bluetooth de fabrica
# ---------------------------------------------------------------------------
# Sem isto o controlador sobe com 00:00:46:67:61:01 -- o marcador que a
# MediaTek deixa quando ninguem gravou nada.  E' o MESMO em toda unidade que
# rode este port, o que impede dois aparelhos iguais de conviverem no mesmo
# host pareado.
#
# O endereco real vive na particao nvdata, em /APCFG/APRDEB/BT_Addr, ao lado
# da calibracao de RF do Wi-Fi.  E' um dado POR UNIDADE: ele e' lido do
# aparelho em tempo de execucao e NUNCA entra no repositorio.
#
# A ordem dos bytes foi determinada por experimento, nao por suposicao:
#   enviado como esta' no nvram -> o controlador passa a responder outro
#                                  endereco, que nao e' o de fabrica
#   enviado invertido           -> Read_BD_ADDR devolve exatamente o do nvram
# O motivo e' que o HCI carrega BD_ADDR com o byte menos significativo
# primeiro, enquanto o nvram guarda na ordem de exibicao.

NVRAM_BD = "/lib/firmware/bt_addr.bin"
OP_RESET = 0x0C03
OP_READ_BD_ADDR = 0x1009
OP_MTK_WRITE_BD_ADDR = 0xFC1A


def comando_direto(fd, opcode, params=b"", tempo=4.0):
    """Manda um comando HCI e espera a resposta dele, antes de existir o vhci.

    Devolve (tipo, dados) com tipo em {"cc","cs"}, ou None se nao respondeu.
    Eventos de outros opcodes sao descartados -- nesta fase ninguem mais
    esta' falando com o controlador.
    """
    os.write(fd, bytes([0x01]) + struct.pack("<H", opcode)
             + bytes([len(params)]) + params)
    buf = bytearray()
    while True:
        r, _, _ = select.select([fd], [], [], tempo)
        if not r:
            return None
        buf += os.read(fd, 1024)
        while True:
            total = tamanho_do_pacote(buf) if buf else None
            if total is None:
                break
            pkt, buf[:] = bytes(buf[:total]), buf[total:]
            if pkt[0] != 0x04 or len(pkt) < 6:
                continue
            if pkt[1] == 0x0E and struct.unpack_from("<H", pkt, 4)[0] == opcode:
                return ("cc", pkt[6:])          # status + parametros
            if pkt[1] == 0x0F and struct.unpack_from("<H", pkt, 5)[0] == opcode:
                return ("cs", pkt[3:4])         # so' o status


def _oui(b):
    """Só o prefixo do fabricante.  O endereco completo e' dado por unidade."""
    return "%02X:%02X:%02X:XX:XX:XX" % (b[0], b[1], b[2])


def aplica_endereco_de_fabrica(fd_bt):
    """Grava o BD_ADDR do nvdata no controlador.  Nao e' fatal se falhar."""
    try:
        alvo = open(NVRAM_BD, "rb").read()[:6]
    except OSError:
        log("sem %s; o controlador fica com o endereco padrao da MediaTek"
            % NVRAM_BD)
        return False
    if len(alvo) != 6 or all(b == 0 for b in alvo) or all(b == 0xFF for b in alvo):
        log("%s nao tem um endereco valido; seguindo sem gravar" % NVRAM_BD)
        return False

    if not comando_direto(fd_bt, OP_RESET):
        log("o controlador nao respondeu ao reset; nao gravei o endereco")
        return False

    r = comando_direto(fd_bt, OP_MTK_WRITE_BD_ADDR, bytes(reversed(alvo)))
    if not r or r[0] != "cc" or r[1][0] != 0:
        log("0xFC1A recusado (%s); endereco de fabrica nao aplicado" % (r,))
        return False

    v = comando_direto(fd_bt, OP_READ_BD_ADDR)
    if not v or v[0] != "cc" or v[1][0] != 0:
        log("nao reli o BD_ADDR para conferir")
        return False
    lido = bytes(reversed(v[1][1:7]))
    if lido != alvo:
        log("conferencia falhou: o controlador ficou com %s" % _oui(lido))
        return False
    log("endereco de fabrica aplicado e conferido: %s" % _oui(lido))
    return True


def cria_controlador(fd_vhci):
    """Registra o hci_dev virtual e devolve o indice (0 para hci0).

    O protocolo do hci_vhci: escrever dois bytes, HCI_VENDOR_PKT (0xff) e o
    tipo de dispositivo (0x00 = HCI_PRIMARY).  O kernel responde com quatro
    bytes: 0xff, o tipo de volta, e o indice em little-endian.

    Ha' uma armadilha de tempo aqui: se nada for escrito em ate' 1 segundo
    depois do open(), o proprio kernel cria um controlador primario por conta
    propria (vhci_open_timeout), e a nossa escrita passa a devolver -EBADFD.
    Por isso isto e' a primeira coisa a acontecer depois do open.
    """
    os.write(fd_vhci, bytes([0xFF, 0x00]))
    resp = os.read(fd_vhci, 4)
    if len(resp) != 4 or resp[0] != 0xFF:
        raise RuntimeError("resposta inesperada do vhci: %s" % resp.hex())
    return int.from_bytes(resp[2:4], "little")


def repassa_do_host(fd_vhci, fd_bt):
    """BlueZ -> chip.  Cada read() do vhci ja' e' um pacote completo."""
    pkt = os.read(fd_vhci, 4096)
    if not pkt:
        return False
    if pkt[0] == 0xFF:
        # Pacote de controle do proprio vhci; nao vai para o chip.
        dbg("controle do vhci: %s" % pkt.hex())
        return True
    dbg("-> chip  %s" % pkt[:24].hex())
    escreve_com_retry(fd_bt, pkt)
    return True


def escreve_com_retry(fd, dados, tentativas=5):
    """send_hci_frame() do driver devolve -EAGAIN quando a fila da STP enche."""
    for n in range(tentativas):
        try:
            os.write(fd, dados)
            return
        except BlockingIOError:
            select.select([], [fd], [], 0.05)
    raise OSError("fila da STP cheia depois de %d tentativas" % tentativas)


def repassa_do_chip(fd_bt, fd_vhci, buf):
    """Chip -> BlueZ.  Aqui e' preciso remontar o enquadramento H4."""
    dados = os.read(fd_bt, 4096)
    if not dados:
        return False
    buf += dados
    while buf:
        total = tamanho_do_pacote(buf)   # ValueError sobe para o chamador
        if total is None:
            break  # pacote incompleto; espera o proximo read
        pkt, buf[:] = bytes(buf[:total]), buf[total:]
        dbg("<- chip  %s" % pkt[:24].hex())
        pkt, mexeu = corrige_features(pkt)
        if mexeu:
            log("LMP_SYNC_TRAIN removido das features (contorno do chip)")
        os.write(fd_vhci, pkt)
    return True


def main():
    # A ordem importa: abrir /dev/stpbt e' o que liga o radio.  O open()
    # chama mtk_wcn_wmt_func_on(WMTDRV_TYPE_BT) la' dentro, o que acorda o
    # CONNSYS (ou apenas soma uma referencia, se o Wi-Fi ja' o ligou) e
    # espera a STP ficar pronta.  So' faz sentido apresentar o controlador
    # ao BlueZ depois que o chip respondeu.
    log("abrindo %s (isto liga o radio)" % STPBT)
    try:
        fd_bt = os.open(STPBT, os.O_RDWR)
    except OSError as e:
        log("nao abri %s: %s" % (STPBT, e))
        if e.errno == 5:
            log("EIO costuma ser o btonflag preso de uma execucao anterior")
            log("que morreu com uma thread em BT_read; so' reiniciando resolve")
        log("o modulo bt_drv_6833 esta carregado?")
        return 1
    log("%s aberto" % STPBT)

    # Antes de apresentar o controlador ao BlueZ: gravar o endereco de
    # fabrica.  Depois que o vhci existe, quem fala com o chip e' o kernel, e
    # intercalar comandos nossos no meio da inicializacao dele daria confusao.
    aplica_endereco_de_fabrica(fd_bt)

    try:
        fd_vhci = os.open(VHCI, os.O_RDWR)
    except OSError as e:
        log("nao abri %s: %s (falta CONFIG_BT_HCIVHCI?)" % (VHCI, e))
        os.close(fd_bt)
        return 1

    try:
        indice = cria_controlador(fd_vhci)
    except Exception as e:
        log("nao criei o controlador: %s" % e)
        os.close(fd_vhci)
        os.close(fd_bt)
        return 1
    log("controlador virtual criado: hci%d" % indice)

    # Autopipe: entra no mesmo select e e' o unico jeito de sair do laco sem
    # matar o processo no meio de um read -- que e' exatamente o que prende o
    # descritor dentro do driver.
    r_parar, w_parar = os.pipe()

    def pede_parada(signum, _frame):
        os.write(w_parar, bytes([signum]))

    signal.signal(signal.SIGTERM, pede_parada)
    signal.signal(signal.SIGINT, pede_parada)

    buf = bytearray()
    motivo = "sinal"
    try:
        while True:
            legiveis, _, _ = select.select([fd_bt, fd_vhci, r_parar], [], [])
            if r_parar in legiveis:
                break
            if fd_vhci in legiveis:
                if not repassa_do_host(fd_vhci, fd_bt):
                    motivo = "o vhci fechou"
                    break
            if fd_bt in legiveis:
                if not repassa_do_chip(fd_bt, fd_vhci, buf):
                    motivo = "o stpbt fechou"
                    break
    except ValueError as e:
        # Fluxo dessincronizado.  Descartar seria pior: o BlueZ ficaria
        # esperando para sempre uma resposta que nunca chega, sem aviso.
        motivo = "enquadramento H4 quebrado: %s" % e
        log(motivo)
    except OSError as e:
        motivo = "erro de E/S: %s" % e
        log(motivo)

    log("encerrando (%s)" % motivo)
    # Ordem: primeiro o vhci, que tira o hci0 do BlueZ; depois o stpbt, cujo
    # close chama BT_release e desliga a funcao de BT no WMT.
    os.close(fd_vhci)
    os.close(fd_bt)
    log("descritores fechados")
    return 0


if __name__ == "__main__":
    sys.exit(main())
