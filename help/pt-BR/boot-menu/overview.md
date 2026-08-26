# Construtor do menu de boot

Monte o menu de inicialização visível a partir de entradas independentes. Escolha um modelo e, em seguida, refine essa entrada usando os controles disponíveis. Entradas desativadas permanecem no projeto, mas são omitidas do menu gerado.

## Menu de origem existente

Antes da personalização, o construtor lê as entradas MiniOS reconhecidas, seus valores padrão e tempo limite, além dos parâmetros suportados, a partir do menu GRUB efetivo ou SYSLINUX nativo. Ao editar uma entrada importada, apenas os parâmetros representados por controles tipados são substituídos. Os demais argumentos de origem permanecem no template de boot. Em um menu multilíngue, cada idioma mantém sua localidade, fuso horário e argumentos de teclado de origem, a menos que você os substitua explicitamente.

## Como uma entrada é montada

O template fornece o comportamento base do MiniOS. Em seguida, são aplicados os argumentos globais de kernel para especialistas, depois as opções tipadas e os Parâmetros adicionais para essa entrada. Para opções MiniOS repetidas no formato chave-valor, o último valor prevalece.

## Templates de sessão

Resume usa perchdir=resume. New usa perchdir=new. Choose usa perchdir=ask.
Fresh não possui seletor de persistência. Copiar para RAM usa toram.

Você pode criar várias entradas a partir do mesmo template.

## Tipos de persistência

Native armazena alterações em um diretório. Dynfilefs usa um contêiner expansível, Raw utiliza uma imagem de tamanho fixo e LUKS utiliza um contêiner criptografado. SquashFS retoma uma sessão comprimida existente. O initramfs atual não pode criar uma nova sessão SquashFS.

## Configurações dependentes

Os controles ficam indisponíveis quando não se aplicam. Desabilitar o zRAM desativa seus controles de compressão e tamanho. As persistências Native e SquashFS não utilizam o campo de tamanho do contêiner.

## Conclusão e entrada avançada

Os filtros de módulos completam a partir dos módulos detectados na fonte selecionada. Os campos de localidade, fuso horário e teclado completam a partir dos dados instalados no sistema. Use Parâmetros adicionais apenas para opções sem controle tipado. Argumentos desconhecidos carregados de um projeto antigo são preservados nesse campo.

## Padrões e nomes

Um menu personalizado possui exatamente uma entrada padrão. Ao desabilitar o padrão, outra entrada habilitada é selecionada automaticamente. Um nome vazio mantém o título da origem ou do template. Nomes personalizados em ASCII funcionam em menus multilíngues; um menu de idioma único pode usar caracteres suportados pela codificação do menu desse bootloader.
