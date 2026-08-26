# Parâmetros de boot e kernel

Digite os parâmetros separados por espaços. A conclusão sugere opções comuns do MiniOS e do Linux. Parâmetros globais se aplicam a todas as entradas de sessão do MiniOS criadas ou preservadas. Parâmetros específicos de entrada são adicionados depois e podem sobrescrever uma opção de chave-valor do MiniOS repetida.

## Sessão e persistência

Essas opções permitem alterações persistentes, retomam a sessão compatível mais recente,
criam uma nova sessão, perguntam na inicialização ou selecionam diretamente uma sessão numerada:
perch, perchdir=resume, perchdir=new, perchdir=ask e perchdir=NUMBER.

Os modos de armazenamento são native, dynfilefs, raw, luks e squashfs. SquashFS pode
retomar uma sessão compactada existente, mas o initramfs atual não pode criar
uma nova. Os tamanhos de contêiner aceitam os sufixos MB, GB ou TB. O espaço livre reservado
é medido em MiB; o padrão é 256 e o máximo é 4096.

As opções correspondentes são perchmode=MODE, perchsize=SIZE e
perchreserve=MIB.

## Copiar para RAM

As opções toram, toram=full e toram=trim copiam o sistema padrão, completo ou
filtrado para a RAM.

## Módulos

O filtro load carrega apenas módulos correspondentes; o filtro noload exclui
módulos correspondentes. Filtros podem conter nomes de módulos, listas ou intervalos do MiniOS
suportados pelo initramfs. As opções são load=FILTER e noload=FILTER.

## Memória e gráficos

As opções de memória desativam o zRAM, escolhem compressão lzo, lzo-rle, lz4, lz4hc ou zstd
e definem o tamanho do zRAM em MiB. O modo texto inicia sem a área de trabalho gráfica. Nomodeset desativa a configuração normal de modo do kernel e é útil
para solução de problemas gráficos. As opções são nozram, zramcomp=ALGORITHM,
zramsize=MIB, text e nomodeset.

## Fonte e localização

Essas opções selecionam a fonte de dados do MiniOS e substituem idioma, fuso horário
e configurações de teclado para a entrada. As opções são from=SOURCE, from=askdisk,
locales=LOCALE, timezone=ZONE e keyboard-layouts=LAYOUT.

## Diagnóstico

Quiet reduz as mensagens de inicialização. Debug ativa diagnósticos adicionais. Use apenas
parâmetros compreendidos pelo Linux, pelo initramfs do MiniOS ou pelo live-config. As
opções são quiet e debug.
