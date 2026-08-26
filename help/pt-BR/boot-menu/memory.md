# Memória

Controle o swap zRAM compactado criado para esta entrada.

## Estado do zRAM

**Automático** mantém os padrões de memória do MiniOS. **Desativado** adiciona `nozram` e
desativa os controles de compressão e tamanho porque eles não se aplicam mais.

## Compressão

`zramcomp=` seleciona o algoritmo de compressão. As opções disponíveis são `lzo`,
`lzo-rle`, `lz4`, `lz4hc` e `zstd`. A disponibilidade do algoritmo também depende
do kernel em execução.

## Tamanho

`zramsize=` define o tamanho do zRAM em MiB. Deixe o campo vazio para que o MiniOS
calcule o tamanho automaticamente. Um valor maior não significa memória física livre:
páginas compactadas ainda consomem RAM.
