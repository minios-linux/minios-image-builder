# Sessão e armazenamento

Escolha como esta entrada localiza e armazena alterações persistentes.

## Seleção de sessão

O modelo de entrada controla se o MiniOS retoma a sessão mais recente, cria uma nova ou pergunta na inicialização. O tipo de armazenamento não substitui essa escolha do modelo.

## Tipo de armazenamento

- **Automático** mantém o modo do modelo ou da sessão salva.
- **Nativo** armazena alterações em um diretório em um sistema de arquivos Linux.
- **Dynfilefs** usa um contêiner expansível.
- **Raw** usa uma imagem de tamanho fixo.
- **LUKS** usa um contêiner criptografado.
- **SquashFS** retoma uma sessão comprimida existente.

O initramfs atual pode retomar, mas não pode criar sessões SquashFS.

## Capacidade

**Tamanho do contêiner** se aplica apenas a sessões baseadas em contêiner, então está desabilitado para Nativo e SquashFS. **Espaço livre a reservar** mantém espaço no dispositivo de persistência para que as alterações salvas não o preencham completamente.

As opções de boot correspondentes são `perchmode=`, `perchsize=` e `perchreserve=`.

## Copiar para RAM

`toram=full` copia todo o sistema para a memória. `toram=trim` copia apenas o conjunto de módulos filtrados. Isso pode permitir a remoção do dispositivo de boot, mas requer RAM suficiente para os dados copiados.
